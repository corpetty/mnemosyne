<script lang="ts">
	// Readers' comments on a live publication, beside the meeting (a tab of its own on a phone):
	// the threads the owner's signed THREADS carries, plus what this page has sent and not yet seen
	// come back ("Sending…", after 15 s "Not sent · Retry"). The look is the guest page's
	// Comments.svelte. With comments off (`canAct` false) the threads are read-only and there is no
	// composer. The model (comments.ts ReaderComments) is not reactive: `rev` changes with it.
	import { tick, untrack } from 'svelte';
	import {
		COUNT_FROM,
		MAX_TEXT,
		anchorKey,
		anchorLine,
		commentCount,
		meetingAnchor,
		sendable,
		textLength,
		type Draft,
		type ReaderComments,
		type ViewComment,
		type ViewThread
	} from './comments';
	import { ago, wasEdited } from '$lib/comments/pure';
	import type { Manifest } from './format';
	import NameCard from './NameCard.svelte';

	let {
		model,
		rev,
		manifest,
		canAct,
		draft,
		focus,
		ondraft,
		ongoto,
		now = () => Date.now()
	}: {
		model: ReaderComments;
		/** Changes whenever `model` notified a change. */
		rev: number;
		manifest: Manifest;
		/** May this reader write (the manifest is live and META says comments are on)? */
		canAct: boolean;
		/** What the composer is anchored to (an affordance in the meeting); null: the meeting. */
		draft: Draft | null;
		/** Threads on this part to bring into view (`anchorKey`); `n` makes repeats count. */
		focus: { key: string; n: number } | null;
		/** The composer's anchor changed (null: back to the meeting). */
		ondraft: (d: Draft | null) => void;
		/** Show this part of the meeting (`anchorKey`). */
		ongoto: (key: string) => void;
		now?: () => number;
	} = $props();

	/** Bumped when a pending op's 15 s run out, and by local changes. */
	let clock = $state(0);
	const shown = $derived.by(() => {
		void rev;
		void clock;
		return model.view();
	});
	const named = $derived.by(() => {
		void rev;
		return model.name !== '';
	});
	/** The owner blocked this reader's rid: no composer, no Reply/Edit/Delete, one line instead. */
	const blocked = $derived.by(() => {
		void rev;
		return model.blockedHere;
	});
	const actionsOpen = $derived(canAct && !blocked);

	// Redraw when the next pending op turns "Not sent".
	$effect(() => {
		void rev;
		void clock;
		const at = untrack(() => model.nextDeadline());
		if (at === null) return;
		const timer = setTimeout(() => clock++, Math.max(0, at - now()) + 10);
		return () => clearTimeout(timer);
	});

	const clip = (s: string) => s.replace(/\s+/g, ' ').trim();
	const LINK = 'hover:text-gray-200';

	function keys(e: KeyboardEvent, go: () => void, stop: () => void) {
		if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !e.isComposing) {
			e.preventDefault();
			go();
		} else if (e.key === 'Escape') {
			e.preventDefault();
			stop();
		}
	}

	// ---- compose -------------------------------------------------------------------------------
	let text = $state('');
	let box = $state<HTMLTextAreaElement>();
	let composeEl = $state<HTMLElement>();
	const draftLine = $derived(draft?.line ? clip(draft.line) : null);

	$effect(() => {
		if (!draft) return;
		void tick().then(() => {
			composeEl?.scrollIntoView?.({ block: 'nearest' });
			box?.focus({ preventScroll: true });
		});
	});

	function send() {
		if (!canAct || !sendable(text)) return;
		if (model.create(draft ?? { anchor: meetingAnchor(), line: null }, text)) {
			text = '';
			ondraft(null);
		}
	}

	function cancelDraft() {
		ondraft(null);
	}

	function setName(name: string) {
		model.setName(name);
		void tick().then(() => box?.focus({ preventScroll: true }));
	}

	// ---- replies, edits, deletes -----------------------------------------------------------------
	let replyingTo = $state<string | null>(null);
	let replyText = $state('');
	let editing = $state<string | null>(null);
	let editText = $state('');
	let confirming = $state<string | null>(null);
	/** Resolved threads opened from their one-line row. */
	let expanded = $state<Record<string, boolean>>({});

	function startReply(t: ViewThread) {
		replyingTo = t.root.id;
		replyText = '';
		editing = confirming = null;
	}
	function sendReply(t: ViewThread) {
		if (!actionsOpen || !sendable(replyText)) return;
		if (model.reply(t.root.id, replyText)) {
			replyingTo = null;
			replyText = '';
		}
	}
	function startEdit(c: ViewComment) {
		editing = c.id;
		editText = c.text;
		replyingTo = confirming = null;
	}
	function saveEdit(c: ViewComment) {
		if (!actionsOpen || !sendable(editText)) return;
		model.edit(c, editText);
		editing = null;
	}
	function remove(c: ViewComment) {
		confirming = null;
		if (actionsOpen) model.remove(c);
	}

	// ---- threads on a part, brought into view ----------------------------------------------------
	let sectionEl = $state<HTMLElement>();
	let flashId = $state<string | null>(null);
	let flashTimer: ReturnType<typeof setTimeout> | null = null;

	$effect(() => {
		const f = focus;
		if (!f) return;
		untrack(() => {
			const t = [...shown.open, ...shown.resolved].find((x) => anchorKey(x.root.anchor) === f.key);
			if (!t) return;
			if (t.root.resolved) expanded[t.root.id] = true;
			void tick().then(() => {
				const el = [...(sectionEl?.querySelectorAll<HTMLElement>('[data-thread]') ?? [])].find((e) => e.dataset.thread === t.root.id);
				el?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' });
				flashId = t.root.id;
				if (flashTimer) clearTimeout(flashTimer);
				flashTimer = setTimeout(() => (flashId = null), 1800);
			});
		});
	});
	$effect(() => () => {
		if (flashTimer) clearTimeout(flashTimer);
	});
</script>

{#snippet counter(value: string)}
	{@const n = textLength(value)}
	{#if n >= COUNT_FROM}
		<span class="text-[11px] {n > MAX_TEXT ? 'text-red-400' : 'text-gray-500'}" data-testid="comment-counter">{n} / {MAX_TEXT}</span>
	{/if}
{/snippet}

{#snippet who(c: ViewComment)}
	{@const state = c.pending ? model.stateOf(c.id) : null}
	<div class="text-xs text-gray-500">
		<span class="font-medium text-gray-300">{c.author || 'Someone'}</span>
		{#if c.waiting}
			· <span class="text-xs text-gray-500" data-testid="comment-waiting">Waiting for the owner</span>
		{:else if state === 'not_sent'}
			· <span class="text-amber-300" data-testid="comment-not-sent"
				>Not sent · <button type="button" class="underline hover:text-amber-200" onclick={() => model.retry(c.id)}>Retry</button></span
			>
		{:else if state === 'sending'}
			· <span class="text-gray-500" data-testid="comment-sending">Sending…</span>
		{:else}
			· {ago(c.created_at, now())}{#if wasEdited(c)} · (edited){/if}
		{/if}
	</div>
{/snippet}

{#snippet entry(c: ViewComment, t: ViewThread, isRoot: boolean, actionable: boolean)}
	{@render who(c)}
	{#if c.deleted}
		<p class="text-gray-600 italic">removed</p>
	{:else if editing === c.id}
		<!-- svelte-ignore a11y_autofocus -->
		<textarea
			bind:value={editText}
			onkeydown={(e) => keys(e, () => saveEdit(c), () => (editing = null))}
			rows="2"
			autofocus
			aria-label="Edit your comment"
			class="w-full resize-none rounded border border-gray-700 bg-gray-800 px-2 py-1.5 text-sm text-gray-200"
		></textarea>
		<div class="flex items-center gap-2 text-[11px]">
			<button
				type="button"
				onclick={() => saveEdit(c)}
				disabled={!sendable(editText)}
				class="rounded bg-blue-600 px-2 py-0.5 text-white hover:bg-blue-500 disabled:opacity-50">Save</button
			>
			<button type="button" onclick={() => (editing = null)} class="text-gray-400 {LINK}">Cancel</button>
			<span class="ml-auto">{@render counter(editText)}</span>
		</div>
	{:else}
		<p class="break-words whitespace-pre-wrap text-gray-200">{c.text}</p>
	{/if}
	{#if !c.deleted && editing !== c.id && actionable && !c.pending && !c.waiting}
		<div class="flex flex-wrap items-center gap-3 text-[11px] text-gray-500">
			{#if isRoot}
				<button type="button" class={LINK} onclick={() => startReply(t)}>Reply</button>
			{/if}
			{#if model.isMine(c)}
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
	{@const key = anchorKey(r.anchor)}
	{@const line = anchorLine(r.anchor, manifest)}
	<div
		data-thread={r.id}
		class="space-y-2 rounded-md border bg-gray-900/80 p-2 text-sm {flashId === r.id ? 'border-yellow-500/70' : 'border-gray-700'}"
	>
		{#if key && line}
			<button
				type="button"
				onclick={() => ongoto(key)}
				title="Show in the meeting"
				class="block w-full truncate text-left text-xs text-gray-500 italic hover:text-gray-300"
				data-testid="thread-anchor">On: {clip(line)}</button
			>
		{/if}
		<div class="space-y-1">{@render entry(r, t, true, actionable)}</div>
		{#if t.replies.length}
			<div class="ml-3 space-y-2 border-l border-gray-700 pl-2">
				{#each t.replies as c (c.id)}
					<div class="space-y-1">{@render entry(c, t, false, actionable)}</div>
				{/each}
			</div>
		{/if}
		{#if replyingTo === r.id && actionable}
			{#if named}
				<!-- svelte-ignore a11y_autofocus -->
				<textarea
					bind:value={replyText}
					onkeydown={(e) => keys(e, () => sendReply(t), () => (replyingTo = null))}
					rows="2"
					autofocus
					placeholder="Reply"
					aria-label="Reply"
					class="w-full resize-none rounded border border-gray-700 bg-gray-800 px-2 py-1.5 text-sm text-gray-200 placeholder:text-gray-600"
				></textarea>
				<div class="flex items-center gap-2 text-[11px]">
					<button
						type="button"
						onclick={() => sendReply(t)}
						disabled={!sendable(replyText)}
						class="rounded bg-blue-600 px-2 py-0.5 text-white hover:bg-blue-500 disabled:opacity-50">Send</button
					>
					<button type="button" onclick={() => (replyingTo = null)} class="text-gray-400 {LINK}">Cancel</button>
					<span class="ml-auto">{@render counter(replyText)}</span>
				</div>
			{:else}
				<NameCard onname={(n) => model.setName(n)} />
			{/if}
		{/if}
	</div>
{/snippet}

<section bind:this={sectionEl} aria-labelledby="reader-comments-heading" data-testid="reader-comments">
	<h2 id="reader-comments-heading" class="text-sm font-semibold text-gray-300">Comments</h2>

	{#if blocked}
		<p class="mt-2 text-xs text-gray-500" data-testid="reader-blocked">{'The owner has stopped taking comments from you on this page.'}</p>
	{:else if canAct}
		<div bind:this={composeEl} class="mt-2 space-y-2 rounded-md border border-gray-600 bg-gray-900 p-2" data-testid="reader-compose">
			{#if !named}
				<NameCard onname={setName} />
			{:else}
				{#if draftLine}
					<p class="truncate text-xs text-gray-500 italic" data-testid="compose-anchor">On: {draftLine}</p>
				{/if}
				<textarea
					bind:this={box}
					bind:value={text}
					onkeydown={(e) => keys(e, send, cancelDraft)}
					rows="2"
					placeholder={draft ? 'Your comment' : 'Comment on this meeting'}
					aria-label={draft ? 'Your comment' : 'Comment on this meeting'}
					class="w-full resize-none rounded border border-gray-700 bg-gray-800 px-2 py-1.5 text-sm text-gray-200 placeholder:text-gray-600"
				></textarea>
				<div class="flex items-center gap-2 text-xs">
					<button
						type="button"
						onclick={send}
						disabled={!sendable(text)}
						class="rounded bg-blue-600 px-2.5 py-1 text-white hover:bg-blue-500 disabled:opacity-50">Send</button
					>
					{#if draft}
						<button type="button" onclick={cancelDraft} class="text-gray-400 {LINK}">Cancel</button>
					{/if}
					<span class="ml-auto">{@render counter(text)}</span>
				</div>
			{/if}
		</div>
	{/if}

	<div class="mt-2 space-y-2">
		{#each shown.open as t (t.root.id)}
			{@render card(t, actionsOpen)}
		{/each}
		{#each shown.resolved as t (t.root.id)}
			{#if expanded[t.root.id]}
				<div class="space-y-1">
					<button
						type="button"
						onclick={() => (expanded[t.root.id] = false)}
						aria-expanded="true"
						class="text-xs text-gray-500 {LINK}">Resolved · {commentCount(1 + t.replies.length)}</button
					>
					{@render card(t, false)}
				</div>
			{:else}
				<button
					type="button"
					data-thread={t.root.id}
					onclick={() => (expanded[t.root.id] = true)}
					aria-expanded="false"
					class="block w-full rounded-md border border-gray-800 px-2 py-1.5 text-left text-xs text-gray-500 {LINK}"
					data-testid="thread-resolved">Resolved · {commentCount(1 + t.replies.length)}</button
				>
			{/if}
		{/each}
	</div>
</section>
