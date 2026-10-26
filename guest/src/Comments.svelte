<script lang="ts">
	// Comments on the notes, beside them (below on a narrow screen): the threads the owner's
	// server seals for guests, plus what this page has sent and not yet seen come back ("Sending…").
	// A guest may start a thread on a selection (Notes.svelte hands the selection over as `draft`),
	// reply, and edit or delete their own; resolving stays with the owner. Read-only (the notes
	// ended): the threads are shown, nothing can be added.
	import { tick, untrack } from 'svelte';
	import {
		PendingOps,
		ago,
		createOp,
		deleteOp,
		editOp,
		isMine,
		mintId,
		orderThreads,
		replyOp,
		wasEdited,
		withPending,
		type Anchor,
		type GuestOp,
		type ViewComment,
		type ViewThread
	} from './comments';
	import Sheet from '$lib/notes/ui/Sheet.svelte';
	import type { GuestDoc } from './doc';
	import { panelId, tabId } from './layout';

	let {
		guest,
		rev,
		canAct,
		draft,
		focus,
		ondone,
		ongoto,
		phone = false,
		visible = true,
		openCount = $bindable(0)
	}: {
		guest: GuestDoc;
		/** Changes whenever `guest` notified a change (GuestDoc is not reactive itself). */
		rev: number;
		/** May this page add, reply, edit or delete? (named, and the notes are not read-only) */
		canAct: boolean;
		/** The selection being commented on, from the notes; null when none. */
		draft: Anchor | null;
		/** A thread to bring into view and flash (a click on its mark in the notes); `n` makes repeats count. */
		focus: { id: string; n: number } | null;
		/** The compose box was sent or cancelled. */
		ondone: () => void;
		/** Show this thread's mark in the notes. */
		ongoto: (id: string) => void;
		/** The phone layout: this panel is a tab's panel, and each thread gets "Show in notes". */
		phone?: boolean;
		/** Phone layout only: is this tab the selected one? */
		visible?: boolean;
		/** Out: the number of open threads, pending ones included (the tab's count). */
		openCount?: number;
	} = $props();

	const pending = new PendingOps();
	/** Bumped whenever `pending` changed (it is not reactive itself). */
	let pRev = $state(0);
	const bump = () => untrack(() => pRev++);

	const threads = $derived.by(() => {
		void rev;
		return guest.threads;
	});
	const shown = $derived.by(() => {
		void rev;
		void pRev;
		return orderThreads(withPending(guest.threads, pending.list, guest.you));
	});
	const nOpen = $derived(shown.open.length);
	$effect(() => {
		openCount = nOpen;
	});
	const empty = $derived(shown.open.length + shown.resolved.length === 0);

	// What a `threads` update shows is no longer pending.
	$effect(() => {
		const ts = threads;
		untrack(() => {
			if (pending.settle(ts)) bump();
		});
	});

	// After a (re)connect's catch-up, what is still not seen goes out again.
	let seenCatchUps = -1; // the count when this panel appeared: not a catch-up it has to answer
	$effect(() => {
		void rev;
		const n = guest.catchUps;
		if (seenCatchUps < 0) seenCatchUps = n;
		if (n === seenCatchUps) return;
		seenCatchUps = n;
		untrack(() => {
			pending.settle(guest.threads);
			for (const op of pending.retry()) void guest.sendComment(op);
			bump();
		});
	});

	function submit(op: GuestOp): void {
		pending.add(op, new Date().toISOString());
		bump();
		void guest.sendComment(op);
	}

	/**
	 * A new thread on `anchor` through the same pending path as the compose box below (shown at once as
	 * "Sending…", sent again after a reconnect): the touch screens' comment sheet. Rejects when this
	 * page may not comment.
	 */
	export async function post(anchor: Anchor, body: string): Promise<void> {
		const t = body.trim();
		if (!canAct) throw new Error('These notes cannot be commented on');
		if (!t) return;
		const op = createOp(mintId(), t, anchor);
		pending.add(op, new Date().toISOString());
		bump();
		// The bridge marks the characters from their ids, so it must have them: what was typed last goes first.
		await guest.synced();
		void guest.sendComment(op);
	}

	// ---- compose (a new thread on the selection) -------------------------------------------------------
	let text = $state('');
	let box = $state<HTMLTextAreaElement>();
	let composeEl = $state<HTMLElement>();

	$effect(() => {
		if (!draft) return;
		void tick().then(() => {
			composeEl?.scrollIntoView?.({ block: 'nearest' });
			box?.focus({ preventScroll: true });
		});
	});

	async function send() {
		const d = draft;
		const t = text.trim();
		if (!d || !t || !canAct) return;
		const op = createOp(mintId(), t, d);
		text = '';
		ondone();
		pending.add(op, new Date().toISOString());
		bump();
		// The bridge marks the characters from their ids, so it must have them: what was typed last goes first.
		await guest.synced();
		void guest.sendComment(op);
	}

	function cancel() {
		text = '';
		ondone();
	}

	function enterSends(e: KeyboardEvent, go: () => void, stop: () => void) {
		if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
			e.preventDefault();
			go();
		} else if (e.key === 'Escape') {
			e.preventDefault();
			stop();
		}
	}

	// ---- replies, edits, deletes -----------------------------------------------------------------------
	let replyingTo = $state<string | null>(null);
	let replyText = $state('');
	let editing = $state<string | null>(null);
	let editText = $state('');
	let confirming = $state<string | null>(null);
	let showResolved = $state(false);

	function startReply(t: ViewThread) {
		replyingTo = t.root.id;
		replyText = '';
		editing = confirming = null;
	}
	function sendReply(t: ViewThread) {
		const body = replyText.trim();
		if (!body || !canAct) return;
		submit(replyOp(mintId(), t.root.id, body));
		replyingTo = null;
		replyText = '';
	}
	function startEdit(c: ViewComment) {
		editing = c.id;
		editText = c.text;
		replyingTo = confirming = null;
	}
	function saveEdit(c: ViewComment) {
		const body = editText.trim();
		editing = null;
		if (!body || body === c.text || !canAct) return;
		submit(editOp(c.id, body));
	}
	function remove(c: ViewComment) {
		confirming = null;
		if (canAct) submit(deleteOp(c.id));
	}

	// ---- a thread to bring into view (a click on its mark) -----------------------------------------------
	let sectionEl = $state<HTMLElement>();
	let flashId = $state<string | null>(null);
	let flashTimer: ReturnType<typeof setTimeout> | null = null;

	$effect(() => {
		const f = focus;
		if (!f) return;
		untrack(() => {
			if (shown.resolved.some((t) => t.root.id === f.id)) showResolved = true;
			void tick().then(() => {
				const el = [...(sectionEl?.querySelectorAll<HTMLElement>('[data-thread]') ?? [])].find((e) => e.dataset.thread === f.id);
				el?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' });
				flashId = f.id;
				if (flashTimer) clearTimeout(flashTimer);
				flashTimer = setTimeout(() => (flashId = null), 1800);
			});
		});
	});
	$effect(() => () => {
		if (flashTimer) clearTimeout(flashTimer);
	});

	function cardClick(e: MouseEvent, t: ViewThread) {
		if (phone) return; // a tap while scrolling must not leave the tab: "Show in notes" does it
		if (e.target instanceof Element && e.target.closest('button, textarea, a, input')) return;
		if (!t.root.pending) ongoto(t.root.id);
	}

	// ---- a thread in a sheet (touch screens: a tap on its mark in the notes) ----------------------------
	/** The roots of the threads the sheet shows (the open ones on the tapped highlight); null when closed. */
	let sheetIds = $state<string[] | null>(null);
	let sheetDrafts = $state<Record<string, string>>({});
	const sheetThreads = $derived.by(() => {
		const ids = sheetIds;
		if (!ids) return [];
		const all = [...shown.open, ...shown.resolved];
		return ids.flatMap((id) => all.filter((t) => t.root.id === id && !t.root.deleted));
	});

	/** Does the last thread in the sheet take a reply (its row then also holds Close)? */
	const lastCanReply = $derived.by(() => {
		const r = sheetThreads.at(-1)?.root;
		return !!r && canAct && !r.resolved && !r.pending && !r.deleted;
	});

	/** Show these threads (root ids, first first) in the bottom sheet. */
	export function openSheet(ids: string[]): void {
		if (!ids.length) return;
		sheetIds = [...ids];
	}
	function closeSheet() {
		sheetIds = null;
	}
	function sendSheetReply(t: ViewThread) {
		const body = (sheetDrafts[t.root.id] ?? '').trim();
		if (!body || !canAct) return;
		submit(replyOp(mintId(), t.root.id, body));
		sheetDrafts[t.root.id] = '';
	}

	const clip = (s: string) => s.replace(/\s+/g, ' ').trim();
	/** Reply, Edit, Delete and the like: small muted text; on a phone the hit area grows (36 px tall, 8 px each side). */
	const LINK = $derived(phone ? 'hover:text-gray-200 inline-flex min-h-9 items-center px-2 -mx-2' : 'hover:text-gray-200');
	/** The gap in a row of such links: wide enough on a phone that neighbouring hit areas only touch. */
	const ROW = $derived(phone ? 'gap-4' : 'gap-3');
	/** Save / Send in the inline edit and reply boxes. */
	const SMALL_PRIMARY = $derived(phone ? 'min-h-9 px-3' : 'px-2 py-0.5');
</script>

{#snippet who(c: ViewComment)}
	<div class="text-xs text-gray-500">
		<span class="font-medium text-gray-300">{isMine(c, guest.you.gid) ? 'You' : c.author || 'Someone'}</span>
		{#if c.pending}
			· <span class="text-amber-400/90" data-testid="comment-sending">Sending…</span>
		{:else}
			· {ago(c.created_at)}{#if wasEdited(c)} · (edited){/if}
		{/if}
	</div>
{/snippet}

{#snippet entry(c: ViewComment, t: ViewThread, isRoot: boolean, actionable: boolean)}
	{@render who(c)}
	{#if c.deleted}
		<p class="text-gray-600 italic">Comment deleted</p>
	{:else if editing === c.id}
		<!-- svelte-ignore a11y_autofocus -->
		<textarea
			bind:value={editText}
			onkeydown={(e) => enterSends(e, () => saveEdit(c), () => (editing = null))}
			rows="2"
			autofocus
			aria-label="Edit your comment"
			class="w-full resize-none rounded border border-gray-700 bg-gray-800 px-2 py-1.5 text-sm text-gray-200"
		></textarea>
		<div class="flex items-center {phone ? 'gap-4' : 'gap-2'} text-[11px]">
			<button
				type="button"
				onclick={() => saveEdit(c)}
				disabled={editText.trim() === ''}
				class="rounded bg-blue-600 {SMALL_PRIMARY} text-white hover:bg-blue-500 disabled:opacity-50">Save</button
			>
			<button type="button" onclick={() => (editing = null)} class="text-gray-400 {LINK}">Cancel</button>
		</div>
	{:else}
		<p class="break-words whitespace-pre-wrap text-gray-200">{c.text}</p>
	{/if}
	{#if !c.deleted && editing !== c.id && actionable && !c.pending}
		<div class="flex flex-wrap items-center {ROW} text-[11px] text-gray-500">
			{#if isRoot}
				<button type="button" class={LINK} onclick={() => startReply(t)}>Reply</button>
			{/if}
			{#if isMine(c, guest.you.gid)}
				<button type="button" class={LINK} onclick={() => startEdit(c)}>Edit</button>
				{#if confirming === c.id}
					<span role="alertdialog" aria-label="Delete this comment?">
						Delete this comment?
						<button type="button" class={LINK} onclick={() => remove(c)}>Yes</button>
						·
						<button type="button" class={LINK} onclick={() => (confirming = null)}>No</button>
					</span>
				{:else}
					<button type="button" class={LINK} onclick={() => (confirming = c.id)}>Delete</button>
				{/if}
			{/if}
		</div>
	{/if}
{/snippet}

{#snippet card(t: ViewThread, actionable: boolean)}
	{@const r = t.root}
	<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
	<div
		data-thread={r.id}
		onclick={(e) => cardClick(e, t)}
		class="space-y-2 rounded-md border bg-gray-900/80 p-2 text-sm {flashId === r.id
			? 'border-yellow-500/70'
			: 'border-gray-700'} {r.pending ? '' : 'cursor-default'}"
	>
		{#if r.quote && !r.deleted}
			<button
				type="button"
				onclick={() => ongoto(r.id)}
				disabled={r.pending || r.detached}
				title={r.detached ? undefined : 'Show in the notes'}
				class="block w-full text-left text-xs text-gray-500 italic {r.detached ? '' : 'hover:text-gray-300'}"
			>
				<span class="line-clamp-2 break-words">“{clip(r.quote)}”</span>
				{#if r.detached}
					<span class="not-italic text-gray-600">(text removed)</span>
				{:else if r.changed}
					<span class="not-italic text-gray-600">(text changed)</span>
				{/if}
			</button>
		{/if}
		<div class="space-y-1">{@render entry(r, t, true, actionable)}</div>
		{#if t.replies.length}
			<div class="ml-3 space-y-2 border-l border-gray-700 pl-2">
				{#each t.replies as c (c.id)}
					<div class="space-y-1">{@render entry(c, t, false, actionable)}</div>
				{/each}
			</div>
		{/if}
		{#if phone && r.quote && !r.deleted && !r.pending && !r.detached}
			<button
				type="button"
				onclick={() => ongoto(r.id)}
				class="min-h-10 rounded border border-gray-700 bg-gray-900 px-3 text-xs text-gray-300">Show in notes</button
			>
		{/if}
		{#if replyingTo === r.id && actionable}
			<!-- svelte-ignore a11y_autofocus -->
			<textarea
				bind:value={replyText}
				onkeydown={(e) => enterSends(e, () => sendReply(t), () => (replyingTo = null))}
				rows="2"
				autofocus
				placeholder="Reply"
				aria-label="Reply"
				class="w-full resize-none rounded border border-gray-700 bg-gray-800 px-2 py-1.5 text-sm text-gray-200 placeholder:text-gray-600"
			></textarea>
			<div class="flex items-center {phone ? 'gap-4' : 'gap-2'} text-[11px]">
				<button
					type="button"
					onclick={() => sendReply(t)}
					disabled={replyText.trim() === ''}
					class="rounded bg-blue-600 {SMALL_PRIMARY} text-white hover:bg-blue-500 disabled:opacity-50">Send</button
				>
				<button type="button" onclick={() => (replyingTo = null)} class="text-gray-400 {LINK}">Cancel</button>
			</div>
		{/if}
	</div>
{/snippet}

{#snippet panel()}
{#if draft || !empty || canAct}
	<section bind:this={sectionEl} aria-labelledby="guest-comments-heading" data-testid="guest-comments">
		<div class="flex items-baseline justify-between gap-2">
			<h2 id="guest-comments-heading" class="text-sm font-semibold text-gray-300">Comments</h2>
			{#if nOpen}<span class="text-xs text-gray-500" data-testid="guest-comments-open">{nOpen} open</span>{/if}
		</div>

		{#if draft && canAct}
			<div bind:this={composeEl} class="mt-2 space-y-2 rounded-md border border-gray-600 bg-gray-900 p-2" data-testid="guest-comment-compose">
				<p class="text-[11px] text-gray-500">Commenting on</p>
				<p class="line-clamp-3 text-xs break-words text-gray-400 italic">“{clip(draft.text)}”</p>
				<textarea
					bind:this={box}
					bind:value={text}
					onkeydown={(e) => enterSends(e, send, cancel)}
					rows="2"
					placeholder="Comment on this text"
					aria-label="Your comment"
					class="w-full resize-none rounded border border-gray-700 bg-gray-800 px-2 py-1.5 text-sm text-gray-200 placeholder:text-gray-600"
				></textarea>
				<div class="flex items-center gap-2 text-xs">
					<button
						type="button"
						onclick={send}
						disabled={text.trim() === ''}
						class="rounded bg-blue-600 px-2.5 py-1 text-white hover:bg-blue-500 disabled:opacity-50">Send</button
					>
					<button type="button" onclick={cancel} class="text-gray-400 {LINK}">Cancel</button>
					<span class="ml-auto text-[11px] text-gray-600">Shift+Enter for a new line</span>
				</div>
			</div>
		{:else if empty && canAct}
			<p class="mt-2 text-xs text-gray-600">Select text in the notes to comment on it.</p>
		{/if}

		<div class="mt-2 space-y-2">
			{#each shown.open as t (t.root.id)}
				{@render card(t, canAct)}
			{/each}
		</div>

		{#if shown.resolved.length}
			<div class="mt-3">
				<button
					type="button"
					onclick={() => (showResolved = !showResolved)}
					aria-expanded={showResolved}
					class="text-xs text-gray-500 {LINK}"
				>
					{showResolved ? '▾' : '▸'} Resolved ({shown.resolved.length})
				</button>
				{#if showResolved}
					<div class="mt-2 space-y-2">
						{#each shown.resolved as t (t.root.id)}
							{@render card(t, false)}
						{/each}
					</div>
				{/if}
			</div>
		{/if}
	</section>
{:else if phone}
	<p class="text-sm text-gray-500" data-testid="guest-comments-none">No comments yet.</p>
{/if}
{/snippet}

{#if phone}
	<div id={panelId('comments')} role="tabpanel" aria-labelledby={tabId('comments')} hidden={!visible}>
		{@render panel()}
	</div>
{:else}
	{@render panel()}
{/if}

{#snippet closeButton()}
	<button type="button" onclick={closeSheet} class="min-h-10 rounded px-3 text-sm text-gray-400 hover:text-gray-200">Close</button>
{/snippet}

{#if sheetThreads.length}
	<Sheet label="Comment thread" testid="thread-sheet" onclose={closeSheet}>
		<div class="space-y-3 pb-1">
			{#each sheetThreads as t, i (t.root.id)}
				{@const r = t.root}
				{@const canReply = canAct && !r.resolved && !r.pending && !r.deleted}
				<div data-sheet-thread={r.id} class="space-y-2 text-sm">
					{#if r.quote && !r.deleted}
						<blockquote
							data-testid="thread-sheet-quote"
							class="line-clamp-2 border-l-2 border-yellow-500/60 pl-2 text-xs break-words text-gray-300"
						>
							{clip(r.quote)}
						</blockquote>
					{/if}
					<div class="space-y-1">{@render entry(r, t, false, false)}</div>
					{#if t.replies.length}
						<div class="ml-3 space-y-2 border-l border-gray-700 pl-2">
							{#each t.replies as c (c.id)}
								<div class="space-y-1">{@render entry(c, t, false, false)}</div>
							{/each}
						</div>
					{/if}
					{#if r.resolved}
						<p class="text-xs text-gray-500">Resolved</p>
					{/if}
					{#if canReply}
						<textarea
							value={sheetDrafts[r.id] ?? ''}
							oninput={(e) => (sheetDrafts[r.id] = e.currentTarget.value)}
							onkeydown={(e) => {
								if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !e.isComposing) {
									e.preventDefault();
									sendSheetReply(t);
								}
							}}
							rows="2"
							placeholder="Reply"
							aria-label="Reply"
							class="w-full resize-none rounded border border-gray-700 bg-gray-800 px-2 py-1.5 text-base text-gray-200 placeholder:text-gray-600"
						></textarea>
						<div class="flex items-center justify-between gap-2">
							{#if i === sheetThreads.length - 1}
								{@render closeButton()}
							{:else}
								<span></span>
							{/if}
							<button
								type="button"
								onclick={() => sendSheetReply(t)}
								disabled={(sheetDrafts[r.id] ?? '').trim() === ''}
								class="min-h-10 rounded bg-blue-600 px-3 text-sm text-white disabled:opacity-40">Send</button
							>
						</div>
					{/if}
				</div>
			{/each}
			{#if !lastCanReply}
				<div class="flex justify-end">{@render closeButton()}</div>
			{/if}
		</div>
	</Sheet>
{/if}
