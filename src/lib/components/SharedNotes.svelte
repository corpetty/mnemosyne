<script lang="ts">
	// The meeting's live shared notes: the notes editor (src/lib/notes/) on the meeting's
	// document, a formatting toolbar, the status line and the link prompt. The document state is
	// shared with the Copilot's "Add to notes" through the registry (stores/notes.svelte.ts).
	import { onMount, tick, untrack } from 'svelte';
	import { ago } from '$lib/app/syncText.js';
	import { commentRange, commentSelection, type CommentSelection } from '$lib/notes/comments.js';
	import { insertTable, toggleTask } from '$lib/notes/commands.js';
	import type { NotesDocState } from '$lib/notes/doc.svelte.js';
	import { PASTE_LIMIT } from '$lib/notes/editor/clipboard.js';
	import '$lib/notes/editor/editor.css';
	import { attachInput, type EditorInput, type LinkRequest, type ToggleKind } from '$lib/notes/editor/input.js';
	import { createEditorView, type EditorView } from '$lib/notes/editor/view.js';
	import { ensureTerminated, lineAt, spansOf, valuesAt, type Doc, type Line, type LineKind } from '$lib/notes/model.js';
	import { UndoStack } from '$lib/notes/undo.js';
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

	const PLACEHOLDER = 'Notes about this meeting… # heading, - list, [ ] task, **bold**';

	const sessionId = $derived(sessionState.activeSession?.id ?? null);

	let docState = $state.raw<NotesDocState | null>(null);
	let rootEl = $state<HTMLElement>();
	let boxEl = $state<HTMLElement>();
	let input = $state.raw<EditorInput | null>(null);
	/** Everything the handlers below need from the mounted editor (not reactive). */
	let ctx: {
		st: NotesDocState;
		doc: Doc;
		view: EditorView;
		input: EditorInput;
		undo: UndoStack;
		root: HTMLElement;
		paint: () => void;
	} | null = null;

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

	// ---- the editor: mounted once the document exists, again if its identity changes ---------------
	$effect(() => {
		const st = docState;
		const doc = st?.doc;
		const root = rootEl;
		if (!st || !doc || !root) return;
		return untrack(() => mount(st, doc, root));
	});

	function mount(st: NotesDocState, doc: Doc, root: HTMLElement): () => void {
		const undo = new UndoStack(doc);
		const refreshEmpty = () => root.toggleAttribute('data-empty', doc.web.textLen(doc.obj) <= 1);
		const changed = () => {
			st.localChange();
			refreshEmpty();
			scheduleToolbar();
		};
		const view = createEditorView({
			root,
			doc,
			readOnly,
			scroller: boxEl,
			onToggleTask: (i) => {
				undo.push(toggleTask(doc, view.lines[i].start).inverse);
				changed();
				view.update();
			}
		});
		// A document nobody typed in has no terminating newline (opening it authors nothing). The
		// first edit adds it, before the input layer reads the DOM: the editor works on terminated
		// lines. Registered before attachInput's own beforeinput listener, so it runs first.
		const onBeforeInput = () => {
			if (inp.readOnly || doc.web.text(doc.obj).endsWith('\n')) return;
			ensureTerminated(doc);
			view.update();
		};
		root.addEventListener('beforeinput', onBeforeInput);
		const inp = attachInput(view, doc, {
			undo,
			readOnly,
			onLocalChange: changed,
			onTyping: () => st.setTyping(true),
			requestLink: openLink,
			onPasteTooLarge: (n) =>
				toastState.error(
					`That paste is too long for the notes (${n.toLocaleString('en-US')} characters; the limit is ${PASTE_LIMIT.toLocaleString('en-US')})`
				)
		});
		const wrap = (apply: () => void) => inp.applyRemote(apply);
		const after = () => {
			refreshEmpty();
			scheduleToolbar();
		};
		st.wrapRemote = wrap;
		st.onRemoteChange = after;
		root.dataset.placeholder = PLACEHOLDER;
		const onBlur = () => st.flush();
		root.addEventListener('focusout', onBlur);
		// Open threads' marks are highlighted, resolved and orphaned ones are plain text. The
		// editor renders the marks without knowing the threads, so the class is put on afterwards,
		// after every render (the observer) and every change of the threads (the effect below).
		const paint = () => {
			const open = commentsState.openNotesIds;
			for (const m of root.querySelectorAll<HTMLElement>('mark.ne-comment')) {
				const on = (m.dataset.comment ?? '').split(' ').some((id) => open.has(id));
				if (m.classList.contains('ne-comment-open') !== on) m.classList.toggle('ne-comment-open', on);
			}
		};
		const painter = new MutationObserver(paint);
		painter.observe(root, { childList: true, subtree: true });
		// A click on a highlight (not the end of a drag) opens its thread.
		const onClick = (e: MouseEvent) => {
			const mark = e.target instanceof Element ? e.target.closest<HTMLElement>('mark.ne-comment-open') : null;
			if (!mark || !root.contains(mark)) return;
			const sel = document.getSelection();
			if (sel && !sel.isCollapsed) return;
			showThreads((mark.dataset.comment ?? '').split(' '));
		};
		root.addEventListener('click', onClick);
		view.update();
		refreshEmpty();
		ctx = { st, doc, view, input: inp, undo, root, paint };
		paint();
		input = inp;
		scheduleToolbar();
		return () => {
			root.removeEventListener('focusout', onBlur);
			root.removeEventListener('click', onClick);
			painter.disconnect();
			root.removeEventListener('beforeinput', onBeforeInput);
			if (st.wrapRemote === wrap) st.wrapRemote = undefined;
			if (st.onRemoteChange === after) st.onRemoteChange = undefined;
			st.flush();
			st.setTyping(false);
			inp.detach();
			view.destroy();
			root.replaceChildren();
			root.removeAttribute('data-empty');
			if (ctx?.view === view) ctx = null;
			input = null;
			linkReq = null;
			draft = null;
			openIds = [];
		};
	}

	$effect(() => {
		const ro = readOnly;
		const i = input;
		if (i) {
			i.setReadOnly(ro);
			if (ro) linkReq = null;
		}
	});

	// ---- toolbar state: the caret's line kind and the marks at the selection -----------------------
	interface Active {
		kind: LineKind | null;
		bold: boolean;
		italic: boolean;
		strike: boolean;
		code: boolean;
		link: boolean;
	}
	const NONE: Active = { kind: null, bold: false, italic: false, strike: false, code: false, link: false };
	let active = $state<Active>(NONE);
	let frame = 0;

	function scheduleToolbar() {
		if (frame || readOnly) return;
		frame = requestAnimationFrame(() => {
			frame = 0;
			computeToolbar();
		});
	}

	function computeToolbar() {
		const c = ctx;
		if (!c) return;
		const sel = c.view.getSelection();
		if (!sel) return; // focus is elsewhere: keep what was shown
		const ls = c.view.lines;
		const from = Math.min(sel.anchor, sel.focus);
		const kind = ls.length ? lineAt(ls as Line[], sel.focus).kind : null;
		const spans = spansOf(c.doc);
		// A caret takes the character before it (typing there continues its marks).
		const pos = from === Math.max(sel.anchor, sel.focus) ? Math.max(0, from - 1) : from;
		const has = (k: string) => valuesAt(spans, pos, k).length > 0;
		const next: Active = {
			kind: kind === 'task_done' ? 'task' : kind,
			bold: has('bold'),
			italic: has('italic'),
			strike: has('strike'),
			code: has('code'),
			link: has('link')
		};
		if (JSON.stringify(next) !== JSON.stringify(active)) active = next;
	}

	onMount(() => {
		const onSelection = () => {
			const root = rootEl;
			const sel = document.getSelection();
			const node = sel?.anchorNode;
			if (root && node && root.contains(node)) {
				selected = !sel.isCollapsed;
				scheduleToolbar();
			}
		};
		document.addEventListener('selectionchange', onSelection);
		// Ctrl+Alt+M comments on the selection, also in a read-only editor (which has no focus of
		// its own to carry the key press).
		const onKey = (e: KeyboardEvent) => {
			if (!(e.ctrlKey && e.altKey && !e.shiftKey && !e.metaKey && e.code === 'KeyM')) return;
			const node = document.getSelection()?.anchorNode;
			if (!rootEl || !node || !rootEl.contains(node)) return;
			e.preventDefault();
			startComment();
		};
		document.addEventListener('keydown', onKey);
		const clock = setInterval(() => (now = Date.now()), 30_000);
		return () => {
			document.removeEventListener('selectionchange', onSelection);
			document.removeEventListener('keydown', onKey);
			clearInterval(clock);
			if (frame) cancelAnimationFrame(frame);
		};
	});

	// ---- toolbar actions ---------------------------------------------------------------------------
	/** A toolbar click with focus elsewhere (keyboard): put the caret at the end first. */
	function focusEditor(): boolean {
		const c = ctx;
		if (!c) return false;
		if (!c.view.getSelection()) {
			c.root.focus({ preventScroll: true });
			c.view.setSelection(Math.max(0, c.doc.web.textLen(c.doc.obj) - 1));
		}
		return true;
	}

	function setKind(kind: LineKind) {
		if (focusEditor()) ctx?.input.setLineKind(kind);
	}
	function toggle(kind: ToggleKind) {
		if (focusEditor()) ctx?.input.toggleInline(kind);
	}

	function addTable() {
		if (!focusEditor() || !ctx) return;
		const { view, doc, undo, st } = ctx;
		const sel = view.getSelection();
		const at = sel ? Math.min(sel.anchor, sel.focus) : Math.max(0, doc.web.textLen(doc.obj) - 1);
		const line = view.lines.length ? lineAt(view.lines as Line[], at) : null;
		if (line?.cells) return; // already in a table: Tab adds rows
		view.beforeChange();
		const r = insertTable(doc, at, 2, 2);
		undo.push(r.inverse, { caret: at });
		st.localChange();
		view.update();
		view.setSelection(r.caret);
	}

	function linkButton() {
		if (!focusEditor() || !ctx) return;
		// Ctrl+K is the editor's own link command; ask it the same way.
		linkReq = null;
		ctx.root.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true, bubbles: true, cancelable: true }));
		if (!linkReq) toastState.info('Select the text to link first');
	}

	// ---- comments ----------------------------------------------------------------------------------
	// Anyone who may comment on the meeting may comment on the notes, a reader too: the editor does
	// not author the mark, it sends the ids of the first and last selected character and the
	// server marks them (the frame comes back like any remote edit).
	const canComment = $derived(
		!!sessionId && commentsState.canComment && commentsState.sessionId === sessionId && !!docState?.doc
	);
	let selected = $state(false);
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

	// Highlights follow the threads: a resolved or deleted thread's mark stops showing.
	$effect(() => {
		void commentsState.openNotesIds;
		ctx?.paint();
	});

	function startComment() {
		const c = ctx;
		if (!c || !canComment) return;
		const sel = c.view.getSelection();
		const picked = sel ? commentSelection(c.doc, sel.anchor, sel.focus) : null;
		if (!picked) {
			toastState.info('Select the text to comment on first');
			return;
		}
		draft = picked;
	}

	async function postComment(text: string) {
		const d = draft;
		const c = ctx;
		if (!d || !c) return;
		// The server marks the characters, so it must have them: what was typed last goes first,
		// and the request waits until the relay has handled it.
		await c.st.synced();
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
		const ready = input;
		if (!id || !ready || !ctx) return;
		const thread = commentsState.byNotesMark.get(id);
		if (!thread && !commentsState.threads.length) return; // not loaded yet
		uiState.focusNotesComment = null;
		untrack(() => goToComment(id, thread?.root.notes_range ?? null));
	});

	function goToComment(id: string, fallback: number[] | null) {
		const c = ctx;
		if (!c) return;
		// The mark in the document, else where the last fold saw it.
		let range = commentRange(c.doc, id);
		if (!range && fallback) {
			const len = Math.max(0, c.doc.web.textLen(c.doc.obj) - 1);
			range = { start: Math.min(fallback[0], len), end: Math.min(fallback[1], len) };
		}
		openIds = [id];
		if (!range || range.start >= range.end) return;
		if (!c.view.readOnly) c.root.focus({ preventScroll: true });
		c.view.setSelection(range.start, range.end);
		void tick().then(() => {
			const el =
				c.root.querySelector<HTMLElement>(`mark.ne-comment[data-comment~="${CSS.escape(id)}"]`) ??
				document.getSelection()?.anchorNode?.parentElement;
			el?.scrollIntoView({ block: 'center' });
		});
	}

	// ---- the link prompt ---------------------------------------------------------------------------
	let linkReq = $state<LinkRequest | null>(null);
	let linkUrl = $state('');
	let linkField = $state<HTMLInputElement>();

	function openLink(r: LinkRequest) {
		linkReq = r;
		linkUrl = r.url ?? '';
		void tick().then(() => {
			linkField?.focus();
			linkField?.select();
		});
	}

	/** Close the prompt, give the editor its focus and selection back; `url` undefined = cancel. */
	function closeLink(url?: string | null) {
		const r = linkReq;
		linkReq = null;
		const c = ctx;
		if (!r || !c) return;
		c.root.focus({ preventScroll: true });
		c.view.setSelection(r.from, r.to);
		if (url !== undefined) c.input.setLink(r.from, r.to, url);
	}

	function applyLink() {
		let url = linkUrl.trim();
		if (!url) {
			closeLink(linkReq?.url ? null : undefined);
			return;
		}
		if (/^(javascript|data|vbscript):/i.test(url)) {
			toastState.error('That kind of link is not allowed');
			return;
		}
		if (!/^[a-z][a-z0-9+.-]*:/i.test(url)) url = 'https://' + url;
		closeLink(url);
	}

	// ---- the status line ---------------------------------------------------------------------------
	let now = $state(Date.now());
	const others = $derived((docState?.present ?? []).filter((n) => n !== docState?.you?.name));
	const guestNames = $derived(docState?.guests ?? []);
	const guestTypers = $derived(docState?.guestsTyping ?? []);
	const typers = $derived((docState?.typing ?? []).filter((n) => n !== docState?.you?.name));
	const lastBy = $derived.by(() => {
		const e = docState?.lastEdit;
		return e && e.by !== docState?.you?.name ? `last edit by ${e.by} ${ago(e.at, now)}` : '';
	});

	const GROUP = 'flex rounded border border-gray-700 overflow-hidden text-xs';
	const btn = (on: boolean) =>
		`px-2 py-1 ${on ? 'bg-gray-700 text-gray-100' : 'bg-gray-900 text-gray-400 hover:text-gray-200'}`;

	const headings = [
		{ kind: 'h1', label: 'H1', name: 'Heading 1', keys: 'Ctrl+Alt+1' },
		{ kind: 'h2', label: 'H2', name: 'Heading 2', keys: 'Ctrl+Alt+2' },
		{ kind: 'h3', label: 'H3', name: 'Heading 3', keys: 'Ctrl+Alt+3' }
	] as const;
	const blocks = [
		{ kind: 'bullet', label: '•', name: 'Bulleted list', keys: 'Ctrl+Shift+8' },
		{ kind: 'number', label: '1.', name: 'Numbered list', keys: 'Ctrl+Shift+7' },
		{ kind: 'task', label: '☐', name: 'Task list', keys: 'Ctrl+Shift+9' },
		{ kind: 'quote', label: '❝', name: 'Quote', keys: 'type > and a space' },
		{ kind: 'code', label: '{ }', name: 'Code block', keys: 'type ``` and Enter' }
	] as const;
	const marks = [
		{ kind: 'bold', label: 'B', name: 'Bold', keys: 'Ctrl+B', cls: 'font-bold' },
		{ kind: 'italic', label: 'I', name: 'Italic', keys: 'Ctrl+I', cls: 'italic' },
		{ kind: 'strike', label: 'S', name: 'Strikethrough', keys: 'Ctrl+Shift+X', cls: 'line-through' },
		{ kind: 'code', label: '</>', name: 'Inline code', keys: 'Ctrl+E', cls: 'font-mono' }
	] as const;
</script>

<div class="space-y-2">
	<div class="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
		{#if !readOnly || canComment}
			<div class="flex flex-wrap gap-1.5" role="toolbar" aria-label={readOnly ? 'Notes' : 'Format notes'}>
				{#if !readOnly}
				<div class={GROUP}>
					{#each headings as h}
						<button
							type="button"
							onmousedown={(e) => e.preventDefault()}
							onclick={() => setKind(h.kind)}
							aria-label={h.name}
							aria-pressed={active.kind === h.kind}
							title="{h.name} ({h.keys})"
							class={btn(active.kind === h.kind)}>{h.label}</button
						>
					{/each}
				</div>
				<div class={GROUP}>
					{#each blocks as b}
						<button
							type="button"
							onmousedown={(e) => e.preventDefault()}
							onclick={() => setKind(b.kind)}
							aria-label={b.name}
							aria-pressed={active.kind === b.kind}
							title="{b.name} ({b.keys})"
							class={btn(active.kind === b.kind)}>{b.label}</button
						>
					{/each}
					<button
						type="button"
						onmousedown={(e) => e.preventDefault()}
						onclick={addTable}
						aria-label="Insert table"
						title="Insert a 2×2 table (or type |a|b| and Enter)"
						class={btn(false)}>⊞</button
					>
				</div>
				<div class={GROUP}>
					{#each marks as m}
						<button
							type="button"
							onmousedown={(e) => e.preventDefault()}
							onclick={() => toggle(m.kind)}
							aria-label={m.name}
							aria-pressed={active[m.kind]}
							title="{m.name} ({m.keys})"
							class="{btn(active[m.kind])} {m.cls}">{m.label}</button
						>
					{/each}
					<button
						type="button"
						onmousedown={(e) => e.preventDefault()}
						onclick={linkButton}
						aria-label="Link"
						aria-pressed={active.link}
						title="Link (Ctrl+K)"
						class={btn(active.link)}>🔗</button
					>
				</div>
				{/if}
				{#if canComment}
					<div class={GROUP}>
						<button
							type="button"
							onmousedown={(e) => e.preventDefault()}
							onclick={startComment}
							disabled={!selected}
							aria-label="Comment on the selection"
							title={selected ? 'Comment on the selected text (Ctrl+Alt+M)' : 'Select text to comment on (Ctrl+Alt+M)'}
							class="{btn(!!draft)} disabled:opacity-40 disabled:hover:text-gray-400">💬 Comment</button
						>
					</div>
				{/if}
			</div>
		{:else}
			<span></span>
		{/if}
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
				{docState.present.join(', ')} here{typers.length
					? ` · ${typers.join(', ')} ${typers.length === 1 ? 'is' : 'are'} typing…`
					: ''}{lastBy ? ` · ${lastBy}` : ''}
			{/if}
			{#if guestNames.length}
				<span class="text-gray-500">
					· Guests here: {guestNames.join(', ')}{guestTypers.length
						? ` · ${guestTypers.join(', ')} ${guestTypers.length === 1 ? 'is' : 'are'} typing…`
						: ''}
				</span>
			{/if}
		</span>
	</div>

	{#if linkReq}
		<form
			aria-label="Link"
			class="flex items-center gap-2"
			onsubmit={(e) => {
				e.preventDefault();
				applyLink();
			}}
		>
			<input
				bind:this={linkField}
				bind:value={linkUrl}
				type="text"
				inputmode="url"
				placeholder="https://…"
				aria-label="Link address"
				onkeydown={(e) => {
					if (e.key === 'Escape') {
						e.preventDefault();
						e.stopPropagation();
						closeLink();
					}
				}}
				class="flex-1 min-w-0 bg-gray-900 border border-gray-700 rounded px-2 py-1 text-sm text-gray-100 placeholder-gray-600 focus:outline-none focus:border-gray-500"
			/>
			<button type="submit" class="shrink-0 px-2.5 py-1 text-xs rounded bg-blue-600 hover:bg-blue-700 text-white">Apply</button>
			{#if linkReq.url}
				<button
					type="button"
					onclick={() => closeLink(null)}
					class="shrink-0 px-2.5 py-1 text-xs rounded border border-gray-700 text-gray-300 hover:text-gray-100">Remove</button
				>
			{/if}
		</form>
	{/if}

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

	<div
		bind:this={boxEl}
		class="min-h-64 px-4 py-3 bg-gray-900 border border-gray-700 rounded-lg focus-within:border-gray-500 {maxHeight}"
	>
		<div bind:this={rootEl} class="min-h-[14.5rem]" data-testid="notes-editor"></div>
	</div>

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

<style>
	:global(.notes-editor) {
		position: relative;
	}
	:global(.notes-editor[data-empty]::before) {
		content: attr(data-placeholder);
		position: absolute;
		inset: 0 0 auto 0;
		color: rgb(75 85 99);
		pointer-events: none;
		font-size: 0.875rem;
		line-height: 1.55;
	}
</style>
