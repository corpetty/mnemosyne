<script lang="ts">
	// The meeting's live shared notes: the shared notes editor (src/lib/notes/ui/NotesEditor.svelte)
	// on the meeting's document, the status line, the comment composer and threads. The document
	// state is shared with the Copilot's "Add to notes" through the registry (stores/notes.svelte.ts).
	import { onMount, untrack } from 'svelte';
	import { createNotesInserts } from '$lib/app/notesInserts.js';
	import { createNotesPresence } from '$lib/app/notesPresence.js';
	import { ago } from '$lib/app/syncText.js';
	import type { CommentSelection } from '$lib/notes/comments.js';
	import type { NotesDocState } from '$lib/notes/doc.svelte.js';
	import { colorsFor } from '$lib/notes/ui/carets.js';
	import NotesEditor from '$lib/notes/ui/NotesEditor.svelte';
	import type { NotesHost } from '$lib/notes/ui/types.js';
	import { commentsState } from '$lib/stores/comments.svelte.js';
	import { acquireNotes, releaseNotes } from '$lib/stores/notes.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { uiState } from '$lib/stores/ui.svelte.js';
	import type { CommentCreate } from '$lib/types/index.js';
	import CommentComposer from './CommentComposer.svelte';
	import CommentThread from './CommentThread.svelte';

	/** Classes that limit the editor's height and make it scroll (the recording tab's card). */
	let { maxHeight = '' }: { maxHeight?: string } = $props();

	const sessionId = $derived(sessionState.activeSession?.id ?? null);

	let docState = $state.raw<NotesDocState | null>(null);
	let editor = $state<ReturnType<typeof NotesEditor>>();
	let ready = $state(false);

	// The server decides (access.can_edit_notes: owner, admin and whoever it is shared with edit).
	const readOnly = $derived(docState?.readOnly ?? false);

	// ---- the document state: one per meeting, shared through the registry ---------------------------
	$effect(() => {
		const id = sessionId;
		if (!id) {
			docState = null;
			return;
		}
		const st = acquireNotes(id);
		docState = st;
		return () => {
			st.flush(); // anything typed in the last 200 ms goes out before the connection may close
			releaseNotes(id);
		};
	});

	// A new document (another meeting, or the same one opened anew) drops the comment state.
	$effect(() => {
		void docState?.doc;
		return () => {
			draft = null;
			openIds = [];
		};
	});

	const presence = $derived(docState ? createNotesPresence(docState) : undefined);
	const inserts = $derived(sessionId ? createNotesInserts(sessionId) : undefined);

	/** The document state the mounted editor is bound to (its remote hooks are on it). */
	let bound: NotesDocState | null = null;
	const target = () => bound ?? docState;

	const host: NotesHost = {
		get doc() {
			return docState?.doc ?? null;
		},
		get readOnly() {
			return readOnly;
		},
		get canComment() {
			return canComment;
		},
		get commentActive() {
			return !!draft;
		},
		undoButtons: false,
		get openComments() {
			return commentsState.openNotesIds;
		},
		localChange: () => target()?.localChange(),
		setTyping: (on) => target()?.setTyping(on),
		flush: () => target()?.flush(),
		bindRemote(hooks) {
			const st = docState;
			if (!st) return () => {};
			bound = st;
			st.wrapRemote = hooks.wrap;
			st.onRemoteChange = hooks.after;
			return () => {
				if (st.wrapRemote === hooks.wrap) st.wrapRemote = undefined;
				if (st.onRemoteChange === hooks.after) st.onRemoteChange = undefined;
				if (bound === st) bound = null;
			};
		},
		compose: (sel) => (draft = sel),
		onMark: (ids) => showThreads(ids),
		say: (message, level) => (level === 'error' ? toastState.error(message) : toastState.info(message)),
		get presence() {
			return presence;
		},
		get inserts() {
			return inserts;
		}
	};

	// ---- comments ----------------------------------------------------------------------------------
	// Anyone who may comment on the meeting may comment on the notes, a reader too: the editor does
	// not author the mark, it sends the ids of the first and last selected character and the
	// server marks them (the frame comes back like any remote edit).
	const canComment = $derived(
		!!sessionId && commentsState.canComment && commentsState.sessionId === sessionId && !!docState?.doc
	);
	/** The selection being commented on. */
	let draft = $state<CommentSelection | null>(null);
	/** Threads shown under the editor: a click on a highlight, or "Go to" in the Comments card. */
	let openIds = $state<string[]>([]);
	const shownThreads = $derived(
		openIds.flatMap((id) => {
			const t = commentsState.byNotesMark.get(id);
			return t ? [t] : [];
		})
	);

	async function postComment(text: string) {
		const d = draft;
		const st = target();
		if (!d || !st) return;
		// The server marks the characters, so it must have them: what was typed last goes first,
		// and the request waits until the relay has handled it.
		await st.synced();
		await commentsState.add({
			target: 'notes',
			target_id: '',
			parent_id: '',
			exact: d.text,
			prefix: '',
			suffix: '',
			notes_ids: [d.first, d.last],
			text
		} as CommentCreate);
	}

	/** Open these threads (the ones still open) under the editor. */
	function showThreads(ids: string[]) {
		openIds = ids.filter((id) => commentsState.openNotesIds.has(id));
	}

	// "Go to" in the Comments card: select the comment's range, scroll to it, open its thread.
	$effect(() => {
		const id = uiState.focusNotesComment;
		const ed = editor;
		if (!id || !ready || !ed) return;
		const thread = commentsState.byNotesMark.get(id);
		if (!thread && !commentsState.threads.length) return; // not loaded yet
		uiState.focusNotesComment = null;
		untrack(() => {
			openIds = [id];
			ed.selectComment(id, thread?.root.notes_range ?? null);
		});
	});

	// ---- the status line ---------------------------------------------------------------------------
	let now = $state(Date.now());
	onMount(() => {
		const clock = setInterval(() => (now = Date.now()), 30_000);
		return () => clearInterval(clock);
	});
	const others = $derived((docState?.present ?? []).filter((n) => n !== docState?.you?.name));
	const guestNames = $derived(docState?.guests ?? []);
	// The colour dot before a name in the status line: the caret colour of that person (the user
	// id, else the name; a guest's id), yours included.
	const whoOfName = (name: string): string => {
		const st = docState;
		if (st?.you && name === st.you.name) return st.you.id || name;
		return st?.members.find((m) => m.name === name)?.who || name;
	};
	const whoOfGuest = (name: string): string => docState?.guestMembers.find((g) => g.name === name)?.key.slice(2) || name;
	const guestTypers = $derived(docState?.guestsTyping ?? []);
	const typers = $derived((docState?.typing ?? []).filter((n) => n !== docState?.you?.name));
	const lastBy = $derived.by(() => {
		const e = docState?.lastEdit;
		return e && e.by !== docState?.you?.name ? `last edit by ${e.by} ${ago(e.at, now)}` : '';
	});
</script>

<div class="space-y-2">
	<NotesEditor bind:this={editor} bind:ready {host} testid="notes-editor" boxClass={maxHeight}>
		{#snippet beside()}
			<span class="text-[11px] text-gray-600" aria-live="polite" data-testid="notes-status">
				{#if !docState || docState.status === 'connecting' || docState.status === 'catching_up'}
					Connecting…
				{:else if docState.status === 'gone'}
					This meeting is no longer available
				{:else if docState.status === 'error'}
					<span class="text-red-400">Notes stopped syncing ({docState.error})</span>
					<button type="button" onclick={() => docState?.reconnect()} class="ml-1 underline text-red-300 hover:text-red-200"
						>Reconnect</button
					>
				{:else if docState.status === 'reconnecting'}
					<span class="text-amber-400">Reconnecting… your edits are kept</span>
				{:else if docState.waitingForSync}
					These notes arrive with the next library sync
				{:else if readOnly}
					Read only
				{:else if others.length === 0}
					Saved as you type{lastBy ? ` · ${lastBy}` : ''}
				{:else}
					{#each docState.present as name, i}{#if i}{', '}{/if}<span
							class="mr-1 inline-block h-1.5 w-1.5 rounded-full align-middle"
							style:background-color={colorsFor(whoOfName(name)).bar}
							data-testid="notes-dot"
							aria-hidden="true"></span>{name}{/each} here{typers.length
						? ` · ${typers.join(', ')} ${typers.length === 1 ? 'is' : 'are'} typing…`
						: ''}{lastBy ? ` · ${lastBy}` : ''}
				{/if}
				{#if guestNames.length}
					<span class="text-gray-500">
						· Guests here: {#each guestNames as name, i}{#if i}{', '}{/if}<span
								class="mr-1 inline-block h-1.5 w-1.5 rounded-full align-middle"
								style:background-color={colorsFor(whoOfGuest(name)).bar}
								data-testid="notes-dot"
								aria-hidden="true"></span>{name}{/each}{guestTypers.length
							? ` · ${guestTypers.join(', ')} ${guestTypers.length === 1 ? 'is' : 'are'} typing…`
							: ''}
					</span>
				{/if}
			</span>
		{/snippet}
		{#snippet above()}
			{#if draft && canComment && sessionId}
				<div class="rounded-md border border-gray-700 bg-gray-900/80 p-2 space-y-2" data-testid="notes-comment-composer">
					<p class="text-[11px] text-gray-500">Commenting on</p>
					<blockquote class="border-l-2 border-yellow-500/60 pl-2 text-xs text-gray-300 whitespace-pre-wrap break-words max-h-24 overflow-y-auto">
						{draft.text}
					</blockquote>
					<CommentComposer
						{sessionId}
						target={{ target: 'notes', target_id: '', exact: draft.text, prefix: '', suffix: '', parent_id: '' }}
						placeholder="Comment on this text"
						autofocus
						onsubmit={postComment}
						onposted={() => (draft = null)}
						oncancel={() => (draft = null)}
					/>
				</div>
			{/if}
		{/snippet}
	</NotesEditor>

	{#if shownThreads.length && sessionId}
		<div class="space-y-2" data-testid="notes-comment-threads" aria-label="Comments on the notes">
			<div class="flex items-center justify-between text-[11px] text-gray-500">
				<span>Comments on the notes</span>
				<button type="button" onclick={() => (openIds = [])} class="hover:text-gray-200">Close</button>
			</div>
			{#each shownThreads as t (t.root.id)}
				<div data-comment-thread={t.root.id}>
					<CommentThread thread={t} {sessionId} />
				</div>
			{/each}
		</div>
	{/if}
</div>
