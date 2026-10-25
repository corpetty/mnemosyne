<script lang="ts">
	// The guest notes page: /n/<room>#<key> (format 1) or #2.<room>.<key>.<endpoint>.<relay>
	// (format 2, link.ts). Reads the room and key from its own address and joins the room at once
	// as a GuestDoc (reading needs no name), through iroh and/or the host (transports.ts). On a first visit the name card
	// (Name.svelte, kept in localStorage by identity.ts) comes up over the notes as soon as the
	// meeting's details are known, so it can say whose notes these are; the editor stays read-only
	// and nothing is announced until it is answered. Beside the notes: the agenda, and comments.
	import { onMount } from 'svelte';
	import Agenda from './Agenda.svelte';
	import type { Anchor } from './comments';
	import { openMarkIds } from './comments';
	import Comments from './Comments.svelte';
	import { ensureWasm } from '$lib/notes/wasm.js';
	import { FULL_MESSAGE, connectingLine, formatHostTime, formatMeetingDate, ownersNotes, peopleLine, savedLine, statusPill, typingLine } from './display';
	import { GuestDoc } from './doc';
	import { deriveKeys, type GuestKeys } from './format';
	import { loadGuestWasm } from './guest-wasm';
	import { Identity } from './identity';
	import { parseLink, type GuestLink } from './link';
	import Name from './Name.svelte';
	import Notes from './Notes.svelte';
	import { transportsFor } from './transports';

	type View =
		| { kind: 'incomplete' }
		| { kind: 'unsupported' }
		| { kind: 'connecting' }
		| { kind: 'joined' };

	const identity = new Identity();

	let view = $state<View>({ kind: 'connecting' });
	let guest = $state.raw<GuestDoc | null>(null);
	/** Bumped on every change GuestDoc reports: everything read from it below depends on it. */
	let rev = $state(0);
	let changingName = $state(false);
	let notes = $state<ReturnType<typeof Notes>>();
	/** The selection being commented on, and the thread to bring into view (a click on its mark). */
	let draft = $state<Anchor | null>(null);
	let focus = $state<{ id: string; n: number } | null>(null);
	let unsubscribe: (() => void) | null = null;
	/** Each open() supersedes the one before (a key pasted in while deriving). */
	let attempt = 0;

	function leave() {
		unsubscribe?.();
		unsubscribe = null;
		guest?.flush();
		guest?.destroy();
		guest = null;
	}

	async function open() {
		const n = ++attempt;
		leave();
		const link = parseLink(window.location);
		if (!link) {
			view = { kind: 'incomplete' };
			return;
		}
		view = { kind: 'connecting' };
		let keys: GuestKeys;
		try {
			[keys] = await Promise.all([deriveKeys(link.key), ensureWasm()]);
		} catch {
			if (n === attempt) view = { kind: 'unsupported' };
			return;
		}
		if (n !== attempt) return;
		join(link, keys);
	}

	/** Join the room now. Without a remembered name (a first visit) the name stays "" until the card is answered. */
	function join(link: GuestLink, keys: GuestKeys) {
		draft = null;
		focus = null;
		const g = new GuestDoc({
			room: link.room,
			keys,
			gid: identity.gid,
			name: identity.name,
			transports: transportsFor(link, { loader: loadGuestWasm })
		});
		unsubscribe = g.subscribe(() => rev++);
		guest = g;
		view = { kind: 'joined' };
		g.connect();
	}

	/** The name card's answer: a first visit's Join, or "Change name". */
	function rename(name: string) {
		identity.name = name;
		guest?.setName(name);
		changingName = false;
	}

	function openThread(ids: string[]) {
		if (ids.length) focus = { id: ids[0], n: (focus?.n ?? 0) + 1 };
	}

	// A snapshot of GuestDoc, taken again on each of its notifications (`rev`).
	const snap = $derived.by(() => {
		void rev;
		const g = guest;
		return {
			status: g?.status ?? 'connecting',
			transport: g?.transport ?? null,
			meta: g?.meta ?? null,
			hasDoc: !!g?.doc,
			others: g?.others ?? [],
			typing: g?.typing ?? [],
			you: g?.you.name ?? '',
			readOnly: g?.readOnly ?? false,
			expires: g?.expires ?? null,
			savedAt: g?.savedAt ?? null,
			dead: g ? g.status === 'ended' || g.status === 'gone' : false,
			openIds: new Set(openMarkIds(g?.threads ?? []))
		};
	});
	const status = $derived(snap.status);
	const meta = $derived(snap.meta);
	const hasDoc = $derived(snap.hasDoc);
	const title = $derived(meta?.title || 'Meeting notes');
	const dateText = $derived(formatMeetingDate(meta?.date));
	const owner = $derived(meta?.owner ?? '');
	const pill = $derived(statusPill(status));
	const others = $derived(snap.others);
	const typing = $derived(typingLine(snap.typing));
	const you = $derived(snap.you);
	const expiresText = $derived(formatHostTime(snap.expires));
	const savedText = $derived(savedLine(snap.savedAt, owner));
	/** A first visit: the room is open for reading but nobody has a name yet. */
	const needName = $derived(!you && !snap.readOnly && !snap.dead);
	/** May this page add, edit or delete comments? */
	const canAct = $derived(!!you && !snap.readOnly && !snap.dead);

	const PILL_TONE = {
		muted: 'border-gray-700 text-gray-400',
		green: 'border-green-800 text-green-300',
		amber: 'border-amber-800 text-amber-300',
		gray: 'border-gray-700 text-gray-400',
		red: 'border-red-900 text-red-300'
	} as const;

	$effect(() => {
		document.title = view.kind === 'joined' && hasDoc ? title : 'Mnemosyne';
	});

	// Nothing to comment on once the notes are read-only.
	$effect(() => {
		if (!canAct) draft = null;
	});

	onMount(() => {
		open();
		// A key pasted in after the page opened.
		const onHash = () => open();
		// What was typed in the last moments goes out before the page goes away.
		const onHide = () => guest?.flush();
		window.addEventListener('hashchange', onHash);
		window.addEventListener('pagehide', onHide);
		return () => {
			window.removeEventListener('hashchange', onHash);
			window.removeEventListener('pagehide', onHide);
			leave();
		};
	});
</script>

{#snippet message(heading: string, detail: string, alert: boolean)}
	<div class="mt-12 text-center" role={alert ? 'alert' : 'status'}>
		<h1 class={alert ? 'text-lg font-semibold text-gray-100' : 'text-sm text-gray-500'}>{heading}</h1>
		{#if detail}<p class="mx-auto mt-2 max-w-md text-sm text-gray-500">{detail}</p>{/if}
	</div>
{/snippet}

<main class="mx-auto w-full max-w-[900px] px-4 py-6 sm:py-10">
	{#if view.kind === 'incomplete'}
		{@render message('This link is incomplete', 'Ask for the whole link: the part after # is what opens the notes.', true)}
	{:else if view.kind === 'unsupported'}
		{@render message("This browser can't open these notes", 'Try a current version of Firefox, Chrome or Safari.', true)}
	{:else if view.kind === 'connecting'}
		{@render message('Connecting…', '', false)}
	{:else if guest && status === 'gone'}
		{@render message('These notes are no longer shared.', '', true)}
	{:else if guest && status === 'error'}
		{@render message("This link doesn't open these notes. Ask for the link again.", '', true)}
	{:else if guest && !hasDoc && status === 'full'}
		{@render message(FULL_MESSAGE, '', true)}
	{:else if guest && !hasDoc && !(needName && meta)}
		{@render message(connectingLine(status, snap.transport, owner), '', false)}
	{:else if guest}
		<!-- The name card (first visit) sits over the notes; everything under it is inert. -->
		<div inert={needName}>
		<header class="space-y-3">
			<div class="flex items-start justify-between gap-3">
				<div class="min-w-0">
					<h1 class="text-xl font-semibold break-words text-gray-100">{title}</h1>
					{#if dateText || owner}
						<p class="mt-1 text-sm text-gray-500">
							{dateText}{dateText && owner ? ' · ' : ''}{ownersNotes(owner)}
						</p>
					{/if}
				</div>
				<span
					class="mt-0.5 inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] whitespace-nowrap {PILL_TONE[pill.tone]}"
					role="status"
					aria-live="polite"
					title={pill.detail || undefined}
					data-testid="guest-status"
				>
					{#if pill.tone === 'green'}<span class="h-1.5 w-1.5 rounded-full bg-green-400" aria-hidden="true"></span>{/if}
					{pill.label}{#if pill.detail}<span class="sr-only">{`: ${pill.detail}`}</span>{/if}
				</span>
			</div>
			<p class="text-xs text-gray-500" data-testid="guest-presence">
				{#if others.length}<span class="text-gray-400">{peopleLine(others)}</span>{/if}{#if typing}<span
						aria-live="polite">{' · '}{typing}</span
					>{/if}{#if you}{#if others.length}<span>{' · '}</span>{/if}<span>You: {you}</span>{#if !snap.readOnly}<span
							>{' · '}</span
						><button
							type="button"
							onclick={() => (changingName = true)}
							class="underline decoration-gray-700 underline-offset-2 hover:text-gray-300">Change name</button
						>{/if}{/if}
			</p>
		</header>

		{#if status === 'saved'}
			<div class="mt-4 rounded-md border border-gray-800 bg-gray-900 px-3 py-2 text-sm text-gray-400" role="status" data-testid="guest-saved">
				{savedText}
			</div>
		{:else if status === 'ended'}
			<div class="mt-4 rounded-md border border-gray-800 bg-gray-900 px-3 py-2 text-sm text-gray-400" role="status">
				These notes ended. You can still read and copy them{expiresText ? ` until ${expiresText}` : ''}.
			</div>
		{:else if status === 'full'}
			<div class="mt-4 rounded-md border border-amber-900/60 bg-gray-900 px-3 py-2 text-sm text-amber-300" role="status">
				{FULL_MESSAGE}
			</div>
		{/if}

		<div class="mt-5 grid gap-6 lg:grid-cols-[minmax(0,1fr)_16rem]">
			<section aria-label="Notes" class="min-w-0">
				<Notes
					bind:this={notes}
					{guest}
					{rev}
					locked={!you}
					openIds={snap.openIds}
					cancomment={canAct}
					oncompose={(a) => (draft = a)}
					onmark={openThread}
				/>
			</section>
			<aside class="min-w-0 space-y-6" aria-label="Agenda and comments">
				<Agenda items={meta?.agenda ?? []} />
				<Comments
					{guest}
					{rev}
					{canAct}
					{draft}
					{focus}
					ondone={() => (draft = null)}
					ongoto={(id) => notes?.goTo(id)}
				/>
			</aside>
		</div>

		<footer class="mt-8 border-t border-gray-800 pt-4 text-xs text-gray-600">
			End-to-end encrypted: the server relaying these notes can't read them.
		</footer>
		</div>

		{#if needName && (meta || hasDoc)}
			<div class="fixed inset-0 z-10 flex items-center justify-center bg-black/60 p-4" role="dialog" aria-modal="true" aria-labelledby="guest-name-heading">
				<Name {owner} onjoin={rename} />
			</div>
		{/if}

		{#if changingName}
			<div class="fixed inset-0 z-10 flex items-center justify-center bg-black/60 p-4" role="dialog" aria-modal="true" aria-labelledby="guest-name-heading">
				<Name {owner} initial={you} onjoin={rename} oncancel={() => (changingName = false)} />
			</div>
		{/if}
	{/if}
</main>
