<script lang="ts">
	import { onMount } from 'svelte';
	import { formatTime } from './display';
	import { Follower, STATUS, statusLine } from './live';
	import { openFile, openHosted, stateMessage, swap, unlockFile, type Source, type ViewState } from './load';
	import Meeting from './Meeting.svelte';

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

	function stopFollowing() {
		follower?.stop();
		follower = null;
		following = false;
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
					if (f === follower) following = f.following;
				}
			}
		);
		follower = f;
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
		};
	});
</script>

<main class="mx-auto w-full max-w-3xl px-4 py-8 sm:py-12">
	{#if view.kind === 'ready'}
		<Meeting manifest={view.manifest} loadClip={view.loadClip} loadImage={view.loadImage} {updated} {status} />
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
