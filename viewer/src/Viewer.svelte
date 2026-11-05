<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { parseLiveKeys } from '$lib/sealed/signed';
	import { ReaderComments as Comments, ReaderIdentity, commentsOpen, type Draft, type MeetingComments } from './comments';
	import { formatTime } from './display';
	import { Follower, STATUS, statusLine, type Meta } from './live';
	import { openFile, openHosted, stateMessage, swap, unlockFile, type Source, type ViewState } from './load';
	import Meeting from './Meeting.svelte';
	import ReaderComments from './ReaderComments.svelte';
	import Tabs from './Tabs.svelte';

	let { source }: { source: Source } = $props();

	let view = $state<ViewState>({ kind: 'loading' });
	let password = $state('');
	let unlocking = $state(false);
	const message = $derived(stateMessage(view));
	/** "14:32" once a newer version was swapped in. */
	let updated = $state<string | null>(null);
	let following = $state(false);
	let follower: Follower | null = null;
	// single.html never polls or joins: it has no network (its CSP allows no requests).
	const status = $derived(
		source.kind === 'file' ? STATUS.file : view.kind === 'ready' && view.hosted ? statusLine(following) : null
	);

	// ---- readers' comments (comments.ts) ---------------------------------------------------------
	let meta = $state<Meta | null>(null);
	let comments = $state.raw<Comments | null>(null);
	/** Bumped whenever `comments` changed (it is not reactive itself). */
	let rev = $state(0);
	let seenThreads: unknown = null;
	let draft = $state<Draft | null>(null);
	let focus = $state<{ key: string; n: number } | null>(null);
	let highlight = $state<string | null>(null);
	let highlightTimer: ReturnType<typeof setTimeout> | null = null;
	let tab = $state<'meeting' | 'comments'>('meeting');

	const live = $derived(view.kind === 'ready' && parseLiveKeys(view.manifest.live) !== null);
	const canComment = $derived(view.kind === 'ready' && commentsOpen(view.manifest, meta));
	/** The comments column (a tab below lg): open for comments, or threads to read. */
	const column = $derived.by(() => {
		void rev;
		return live && !!comments && (canComment || comments.threads.length > 0 || comments.pending.list.length > 0);
	});
	const openThreads = $derived.by(() => {
		void rev;
		return comments ? comments.view().open.length : 0;
	});
	const affordances = $derived.by((): MeetingComments | null => {
		void rev;
		if (!canComment || !comments) return null;
		return {
			counts: comments.counts(),
			onComment: (d) => {
				draft = d;
				tab = 'comments';
			},
			onShow: (key) => {
				focus = { key, n: (focus?.n ?? 0) + 1 };
				tab = 'comments';
			}
		};
	});

	/** Show a part of the meeting a thread is on, tinted for 1.5 s. */
	async function goto(key: string) {
		tab = 'meeting';
		await tick();
		const el = [...document.querySelectorAll<HTMLElement>('[data-anchor]')].find((e) => e.dataset.anchor === key);
		el?.scrollIntoView?.({ block: 'center', behavior: 'smooth' });
		highlight = key;
		if (highlightTimer) clearTimeout(highlightTimer);
		highlightTimer = setTimeout(() => (highlight = null), 1500);
	}

	function stopFollowing() {
		follower?.stop();
		follower = null;
		following = false;
		meta = null;
		comments = null;
		seenThreads = null;
		draft = focus = null;
	}

	/** Poll the publication and, when it is live, follow its room; newer versions replace it in place. */
	function follow() {
		if (view.kind !== 'ready' || !view.hosted) return;
		const f: Follower = new Follower(
			view.hosted,
			{ manifest: view.manifest, etag: view.etag ?? '', room: view.room ?? null },
			{
				onManifest: (next) => {
					if (f !== follower || view.kind !== 'ready') return;
					const now = new Date();
					view = swap(view, next, now);
					if (view.kind === 'ready') updated = formatTime(now);
					else stopFollowing();
				},
				onGone: (state) => {
					if (f !== follower) return;
					stopFollowing();
					view = state;
				},
				onChange: () => {
					if (f !== follower) return;
					following = f.following;
					meta = f.meta;
					if (comments && f.threads !== seenThreads) {
						seenThreads = f.threads;
						comments.setThreads(f.threads?.threads);
					}
				}
			}
		);
		follower = f;
		const c = new Comments(
			new ReaderIdentity(view.manifest.id),
			(op, key) => void f.sendComment(op, key),
			undefined,
			() => {
				if (comments === c) rev++;
			}
		);
		comments = c;
		void c.init();
		f.start();
	}

	async function load() {
		stopFollowing();
		updated = null;
		view = { kind: 'loading' };
		view =
			source.kind === 'file'
				? await openFile(source.payload)
				: await openHosted(window.location, window.fetch.bind(window));
		follow();
	}

	async function unlock(e: SubmitEvent) {
		e.preventDefault();
		if (source.kind !== 'file' || unlocking || !password) return;
		unlocking = true;
		try {
			view = await unlockFile(source.payload, password);
		} finally {
			unlocking = false;
		}
	}

	$effect(() => {
		document.title = view.kind === 'ready' ? view.manifest.title || 'Meeting' : 'Mnemosyne';
	});

	onMount(() => {
		load();
		// A key pasted in after the page opened.
		const onHash = () => source.kind === 'hosted' && load();
		window.addEventListener('hashchange', onHash);
		return () => {
			window.removeEventListener('hashchange', onHash);
			stopFollowing();
			if (highlightTimer) clearTimeout(highlightTimer);
		};
	});
</script>

<!-- With comments the meeting keeps its width (3xl) and the column is added beside it from lg up. -->
<main class="mx-auto w-full max-w-3xl px-4 py-8 sm:py-12 {column ? 'lg:max-w-[70rem]' : ''}">
	{#if view.kind === 'ready'}
		<!-- One tree with and without the column, so the meeting (a playing clip) is never mounted again. -->
		{#if column}
			<div class="mb-4 lg:hidden"><Tabs bind:selected={tab} comments={openThreads} /></div>
		{/if}
		<div class={column ? 'lg:grid lg:grid-cols-[minmax(0,1fr)_20rem] lg:gap-8' : ''}>
			<div
				id="viewer-panel-meeting"
				role={column ? 'tabpanel' : undefined}
				aria-labelledby={column ? 'viewer-tab-meeting' : undefined}
				class={column && tab !== 'meeting' ? 'hidden lg:block' : ''}
			>
				<Meeting
					manifest={view.manifest}
					loadClip={view.loadClip}
					loadImage={view.loadImage}
					{updated}
					{status}
					comments={affordances}
					{highlight}
				/>
			</div>
			{#if column && comments}
				<div
					id="viewer-panel-comments"
					role="tabpanel"
					aria-labelledby="viewer-tab-comments"
					class="{tab === 'comments'
						? ''
						: 'hidden'} lg:sticky lg:top-6 lg:block lg:max-h-[calc(100vh-3rem)] lg:self-start lg:overflow-y-auto"
				>
					<ReaderComments
						model={comments}
						{rev}
						manifest={view.manifest}
						canAct={canComment}
						{draft}
						{focus}
						ondraft={(d) => (draft = d)}
						ongoto={goto}
					/>
				</div>
			{/if}
		</div>
	{:else if view.kind === 'password'}
		<form class="mx-auto mt-12 max-w-sm space-y-3" onsubmit={unlock}>
			<h1 class="text-lg font-semibold text-gray-100">This meeting is protected with a password</h1>
			<label class="block text-sm text-gray-400">
				Password
				<!-- svelte-ignore a11y_autofocus -->
				<input
					type="password"
					bind:value={password}
					autocomplete="off"
					autofocus
					class="mt-1 block w-full rounded border border-gray-700 bg-gray-900 px-3 py-2 text-sm text-gray-100 focus:border-gray-500 focus:outline-none"
				/>
			</label>
			<button
				type="submit"
				disabled={unlocking || !password}
				class="w-full rounded border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-200 hover:bg-gray-700 disabled:opacity-50"
				>{unlocking ? 'Opening…' : 'Open'}</button
			>
			{#if view.error}<p class="text-sm text-red-400" role="alert">{view.error}</p>{/if}
		</form>
	{:else if message}
		<div class="mt-12 text-center" role={view.kind === 'loading' ? 'status' : 'alert'}>
			<h1 class={view.kind === 'loading' ? 'text-sm text-gray-500' : 'text-lg font-semibold text-gray-100'}>
				{message.title}
			</h1>
			{#if message.detail}<p class="mx-auto mt-2 max-w-md text-sm text-gray-500">{message.detail}</p>{/if}
		</div>
	{/if}
</main>
