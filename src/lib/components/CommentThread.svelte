<script lang="ts">
	import {
		addComment,
		approveComment,
		deleteComment,
		editComment,
		hideComment,
		reopenComment,
		resolveComment
	} from '$lib/api/backend.js';
	import { ago } from '$lib/app/syncText.js';
	import { commentsState } from '$lib/stores/comments.svelte.js';
	import { publishingState } from '$lib/stores/publishing.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { Comment, CommentCreate, CommentThread } from '$lib/types/index.js';
	import { contactsState } from '$lib/stores/contacts.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import CommentComposer from './CommentComposer.svelte';

	let {
		thread,
		sessionId,
		compact = false,
		onchange
	}: {
		thread: CommentThread;
		sessionId: string;
		compact?: boolean;
		/** Given where the thread is not the open meeting's (Tasks): actions go to sessionId, then this runs. */
		onchange?: () => void | Promise<void>;
	} = $props();

	// commentsState holds the open meeting only; with onchange the calls name this thread's meeting.
	const canReply = $derived(onchange ? true : commentsState.canComment);
	const api = {
		edit: (cid: string, text: string) =>
			onchange ? editComment(sessionId, cid, text).then(() => onchange()) : commentsState.edit(cid, text),
		remove: (cid: string) =>
			onchange ? deleteComment(sessionId, cid).then(() => onchange()) : commentsState.remove(cid),
		resolve: (cid: string) =>
			onchange ? resolveComment(sessionId, cid).then(() => onchange()) : commentsState.resolve(cid),
		reopen: (cid: string) =>
			onchange ? reopenComment(sessionId, cid).then(() => onchange()) : commentsState.reopen(cid),
		approve: (cid: string) =>
			onchange ? approveComment(sessionId, cid).then(() => onchange()) : commentsState.approve(cid),
		hide: (cid: string) =>
			onchange ? hideComment(sessionId, cid).then(() => onchange()) : commentsState.hide(cid)
	};

	// Moderating what readers of the published link write (services/live_publications.py). The
	// meeting's publication is in the publishing store while the meeting is open; if it is not
	// there (yet), every reader's comment that is not approved offers Approve.
	const publication = $derived(publishingState.publications[sessionId]);
	const fromReader = (c: Comment) => c.author_id.startsWith('reader:');
	/** Readers see this comment only once it is approved (`moderate` on, and it was written after that). */
	function needsApproval(c: Comment): boolean {
		if (!fromReader(c) || c.approved_at) return false;
		if (publication === undefined) return true;
		const sel = publication && !publication.unpublished_at ? publication.selection : null;
		if (!sel?.comments || !sel.moderate) return false;
		const since = sel.moderated_since ? Date.parse(sel.moderated_since) : NaN;
		return Number.isNaN(since) || Date.parse(c.updated_at) >= since;
	}
	/** "pending" (waiting for the owner), "hidden" (kept from readers), else null. */
	function moderation(c: Comment): 'pending' | 'hidden' | null {
		if (!fromReader(c) || c.deleted) return null;
		if (c.hidden_at) return 'hidden';
		return needsApproval(c) ? 'pending' : null;
	}
	/** Hide stays offered on a comment readers can see: the owner's kill switch for one thread. */
	const readersComment = $derived(publication === undefined || (!!publication && !publication.unpublished_at && publication.selection.comments));
	async function submitReply(text: string) {
		await addComment(sessionId, { ...replyTarget, text } as CommentCreate);
		await onchange?.();
	}

	const root = $derived(thread.root);
	// The meeting's owner when it was shared with me by someone else: their comments carry no author id.
	const sharedFrom = $derived(
		sessionState.activeSession?.id === sessionId ? (sessionState.activeSession.shared_from ?? '') : ''
	);
	function who(c: Comment): string {
		if (contactsState.isMe(c.author_id)) return 'You';
		if (!c.author_id && sharedFrom) return contactsState.name(sharedFrom);
		return c.author || 'You';
	}
	let expanded = $state(false);
	let replying = $state(false);
	let editing = $state<string | null>(null);
	let draft = $state('');
	let confirming = $state<string | null>(null);

	const collapsed = $derived(!!root.resolved_at && !root.deleted && !expanded);

	function formatTime(seconds: number): string {
		const m = Math.floor(seconds / 60);
		const s = Math.floor(seconds % 60);
		return `${m}:${String(s).padStart(2, '0')}`;
	}

	function clip(s: string, n: number): string {
		const t = s.replace(/\s+/g, ' ').trim();
		return t.length > n ? `${t.slice(0, n)}…` : t;
	}

	function edited(c: Comment): boolean {
		return new Date(c.updated_at).getTime() - new Date(c.created_at).getTime() > 2000;
	}

	const replyTarget = $derived({
		exact: '',
		parent_id: root.id,
		prefix: '',
		suffix: '',
		target: root.target,
		target_id: ''
	} as Omit<CommentCreate, 'text'>);

	async function run(fn: () => Promise<unknown>, fallback: string) {
		try {
			await fn();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : fallback);
		}
	}

	function startEdit(c: Comment) {
		editing = c.id;
		draft = c.text;
		confirming = null;
	}

	async function saveEdit(c: Comment) {
		const text = draft.trim();
		if (!text) return;
		await run(() => api.edit(c.id, text), 'Could not save the comment');
		editing = null;
	}

	function editKey(e: KeyboardEvent, c: Comment) {
		if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
			e.preventDefault();
			saveEdit(c);
		} else if (e.key === 'Escape') {
			e.preventDefault();
			editing = null;
		}
	}

	async function remove(c: Comment) {
		confirming = null;
		await run(() => api.remove(c.id), 'Could not delete the comment');
	}
</script>

{#snippet header(c: Comment)}
	<div class="text-xs text-gray-500">
		<span class="text-gray-300 font-medium">{who(c)}</span>
		· {ago(c.created_at)}{#if edited(c)} · (edited){/if}
	</div>
{/snippet}

{#snippet entry(c: Comment, isRoot: boolean)}
	{@render header(c)}
	{@const mod = moderation(c)}
	{#if fromReader(c) && (isRoot || mod)}
		<!-- A reader of the published link wrote it (services/live_publications.py): they see the replies. -->
		<p class="text-[11px] text-gray-500">
			From the published link{#if isRoot} · replies are shown to its readers{/if}{#if mod === 'pending'}<span class="text-amber-300"> · waiting for your approval</span>{:else if mod === 'hidden'}{' '}· hidden from readers{/if}
		</p>
	{/if}
	{#if c.deleted}
		<p class="text-gray-600 italic">Comment deleted</p>
	{:else if editing === c.id}
		<!-- svelte-ignore a11y_autofocus -->
		<textarea
			bind:value={draft}
			onkeydown={(e) => editKey(e, c)}
			rows="2"
			autofocus
			class="w-full resize-none bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200"
		></textarea>
		<div class="flex items-center gap-2 text-[11px]">
			<button
				onclick={() => saveEdit(c)}
				disabled={draft.trim() === ''}
				class="px-2 py-0.5 rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50"
			>Save</button>
			<button onclick={() => (editing = null)} class="text-gray-400 hover:text-gray-200">Cancel</button>
		</div>
	{:else}
		<p class="text-gray-200 whitespace-pre-wrap break-words">{c.text}</p>
	{/if}
	{#if isRoot && !c.deleted}
		{#if c.changed}
			<p class="text-[11px] text-gray-500 italic">The text changed since: “{clip(c.quote, 120)}”</p>
		{:else if c.detached}
			{#if c.at_seconds != null && c.segment_idx == null}
				<p class="text-[11px] text-gray-500 italic">At {formatTime(c.at_seconds)}</p>
			{:else}
				<p class="text-[11px] text-gray-500 italic">On text that is no longer there: “{clip(c.quote, 120)}”</p>
			{/if}
		{/if}
	{/if}
	{#if !c.deleted && editing !== c.id}
		<div class="flex items-center gap-3 text-[11px] text-gray-500">
			{#if isRoot && canReply}
				<button class="hover:text-gray-200" onclick={() => (replying = !replying)}>Reply</button>
			{/if}
			{#if c.can_edit}
				<button class="hover:text-gray-200" onclick={() => startEdit(c)}>Edit</button>
			{/if}
			{#if c.can_delete}
				{#if confirming === c.id}
					<span>
						Delete?
						<button class="hover:text-gray-200" onclick={() => remove(c)}>Yes</button>
						·
						<button class="hover:text-gray-200" onclick={() => (confirming = null)}>No</button>
					</span>
				{:else}
					<button class="hover:text-gray-200" onclick={() => (confirming = c.id)}>Delete</button>
				{/if}
			{/if}
			{#if c.can_moderate && fromReader(c)}
				{#if mod === 'hidden'}
					<button class="hover:text-gray-200" onclick={() => run(() => api.approve(c.id), 'Could not show it to readers')}>Show to readers</button>
				{:else}
					{#if mod === 'pending'}
						<button class="text-emerald-400 hover:text-emerald-300" onclick={() => run(() => api.approve(c.id), 'Could not approve')}>Approve</button>
					{/if}
					{#if mod === 'pending' || readersComment}
						<button class="hover:text-gray-200" onclick={() => run(() => api.hide(c.id), 'Could not hide it')}>Hide</button>
					{/if}
				{/if}
			{/if}
			{#if isRoot && c.can_resolve}
				{#if c.resolved_at}
					<button class="hover:text-gray-200" onclick={() => run(() => api.reopen(c.id), 'Could not reopen')}>Reopen</button>
				{:else}
					<button class="hover:text-gray-200" onclick={() => run(() => api.resolve(c.id), 'Could not resolve')}>Resolve</button>
				{/if}
			{/if}
		</div>
	{/if}
{/snippet}

{#if collapsed}
	<button
		onclick={() => (expanded = true)}
		class="w-full text-left rounded-md border border-gray-700 bg-gray-900/80 px-2 py-1 text-xs text-gray-500 truncate hover:text-gray-300"
	>
		✓ Resolved by {root.resolved_by || 'You'} · {clip(root.text, compact ? 60 : 100)}
	</button>
{:else}
	<div class="rounded-md border border-gray-700 bg-gray-900/80 p-2 text-sm space-y-2">
		{#if root.resolved_at && !root.deleted}
			<button onclick={() => (expanded = false)} class="text-[11px] text-gray-500 hover:text-gray-200">
				✓ Resolved by {root.resolved_by || 'You'} · collapse
			</button>
		{/if}
		<div class="space-y-1">{@render entry(root, true)}</div>
		{#if thread.replies.length}
			<div class="ml-3 pl-2 border-l border-gray-700 space-y-2">
				{#each thread.replies as r (r.id)}
					<div class="space-y-1">{@render entry(r, false)}</div>
				{/each}
			</div>
		{/if}
		{#if replying && canReply}
			<CommentComposer
				{sessionId}
				target={replyTarget}
				placeholder="Reply"
				autofocus
				onsubmit={onchange ? submitReply : undefined}
				onposted={() => (replying = false)}
				oncancel={() => (replying = false)}
			/>
		{/if}
	</div>
{/if}
