<script lang="ts">
	// The guest page's notes: the app's notes editor (src/lib/notes/editor) on a GuestDoc,
	// mounted the way the app's SharedNotes.svelte mounts it on NotesDocState: the same view,
	// input layer (shortcuts, input rules, markdown paste and copy), local undo of one's own edits,
	// and `wrapRemote`/`onRemoteChange` so a remote edit keeps the caret where it was. No app
	// stores: messages show in a line under the toolbar instead of the app's toasts.
	import { onMount, tick, untrack } from 'svelte';
	import { commentRange, commentSelection } from '$lib/notes/comments.js';
	import { insertTable, toggleTask } from '$lib/notes/commands.js';
	import { PASTE_LIMIT } from '$lib/notes/editor/clipboard.js';
	import '$lib/notes/editor/editor.css';
	import { attachInput, type EditorInput, type LinkRequest, type ToggleKind } from '$lib/notes/editor/input.js';
	import { createEditorView, type EditorView } from '$lib/notes/editor/view.js';
	import { ensureTerminated, lineAt, spansOf, valuesAt, type Doc, type Line, type LineKind } from '$lib/notes/model.js';
	import { UndoStack } from '$lib/notes/undo.js';
	import type { Anchor } from './comments';
	import type { GuestDoc } from './doc';

	let {
		guest,
		rev,
		locked = false,
		openIds = new Set<string>(),
		cancomment = false,
		oncompose,
		onmark
	}: {
		guest: GuestDoc;
		/** Changes whenever `guest` notified a change (GuestDoc is not reactive itself). */
		rev: number;
		/** Read-only until the guest has a name (the first visit's name card). */
		locked?: boolean;
		/** Ids of the threads that are open: their marks are highlighted. */
		openIds?: ReadonlySet<string>;
		/** May the selection be commented on? Shows the Comment button and answers Ctrl+Alt+M. */
		cancomment?: boolean;
		/** The selection to comment on (its text and the ids of its first and last character). */
		oncompose?: (anchor: Anchor) => void;
		/** A click on a highlighted mark: the open threads on it, first one first. */
		onmark?: (ids: string[]) => void;
	} = $props();

	const PLACEHOLDER = 'Notes about this meeting… # heading, - list, [ ] task, **bold**';

	const doc = $derived.by(() => {
		void rev;
		return guest.doc;
	});
	const readOnly = $derived.by(() => {
		void rev;
		const s = guest.status;
		return locked || guest.readOnly || s === 'ended' || s === 'gone';
	});

	let rootEl = $state<HTMLElement>();
	let boxEl = $state<HTMLElement>();
	let input = $state.raw<EditorInput | null>(null);
	/** Everything the handlers below need from the mounted editor (not reactive). */
	let ctx: {
		doc: Doc;
		view: EditorView;
		input: EditorInput;
		undo: UndoStack;
		root: HTMLElement;
		paint: () => void;
	} | null = null;
	let canUndo = $state(false);
	let canRedo = $state(false);

	// ---- the editor: mounted once the document exists, again if its identity changes ---------------
	$effect(() => {
		const g = guest;
		const d = doc;
		const root = rootEl;
		if (!d || !root) return;
		return untrack(() => mount(g, d, root));
	});

	function mount(g: GuestDoc, doc: Doc, root: HTMLElement): () => void {
		const undo = new UndoStack(doc);
		const refreshEmpty = () => root.toggleAttribute('data-empty', doc.web.textLen(doc.obj) <= 1);
		const refreshUndo = () => {
			canUndo = undo.canUndo();
			canRedo = undo.canRedo();
		};
		const changed = () => {
			g.localChange();
			refreshEmpty();
			refreshUndo();
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
			onTyping: () => g.setTyping(true),
			requestLink: openLink,
			onPasteTooLarge: (n) =>
				say(
					`That paste is too long for the notes (${n.toLocaleString('en-US')} characters; the limit is ${PASTE_LIMIT.toLocaleString('en-US')})`
				)
		});
		const wrap = (apply: () => void) => inp.applyRemote(apply);
		const after = () => {
			refreshEmpty();
			refreshUndo();
			scheduleToolbar();
		};
		g.wrapRemote = wrap;
		g.onRemoteChange = after;
		root.dataset.placeholder = PLACEHOLDER;
		const onBlur = () => g.flush();
		root.addEventListener('focusout', onBlur);
		// Open threads' marks are highlighted, resolved and orphaned ones are plain text. The
		// editor renders the marks without knowing the threads, so the class is put on afterwards,
		// after every render (the observer) and every change of the open threads (the effect below).
		const paint = () => {
			for (const m of root.querySelectorAll<HTMLElement>('mark.ne-comment')) {
				const on = (m.dataset.comment ?? '').split(' ').some((id) => openIds.has(id));
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
			const ids = (mark.dataset.comment ?? '').split(' ').filter((id) => openIds.has(id));
			if (ids.length) onmark?.(ids);
		};
		root.addEventListener('click', onClick);
		view.update();
		refreshEmpty();
		refreshUndo();
		ctx = { doc, view, input: inp, undo, root, paint };
		paint();
		input = inp;
		scheduleToolbar();
		return () => {
			root.removeEventListener('focusout', onBlur);
			root.removeEventListener('click', onClick);
			painter.disconnect();
			root.removeEventListener('beforeinput', onBeforeInput);
			if (g.wrapRemote === wrap) g.wrapRemote = undefined;
			if (g.onRemoteChange === after) g.onRemoteChange = undefined;
			g.flush();
			g.setTyping(false);
			inp.detach();
			view.destroy();
			root.replaceChildren();
			root.removeAttribute('data-empty');
			if (ctx?.view === view) ctx = null;
			input = null;
			linkReq = null;
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

	// Highlights follow the open threads: a resolved or deleted thread's mark stops showing.
	$effect(() => {
		void openIds;
		ctx?.paint();
	});

	// ---- messages (the app shows these as toasts) --------------------------------------------------
	let notice = $state('');
	let noticeTimer: ReturnType<typeof setTimeout> | null = null;
	function say(text: string) {
		notice = text;
		if (noticeTimer) clearTimeout(noticeTimer);
		noticeTimer = setTimeout(() => (notice = ''), 5000);
	}

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

	/** Is there text selected in the editor? (shows the Comment button) */
	let selected = $state(false);

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
		return () => {
			document.removeEventListener('selectionchange', onSelection);
			document.removeEventListener('keydown', onKey);
			if (frame) cancelAnimationFrame(frame);
			if (noticeTimer) clearTimeout(noticeTimer);
			if (flashTimer) clearTimeout(flashTimer);
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

	// ---- comments ----------------------------------------------------------------------------------
	// A comment is made from the selection: its text and the ids of its first and last character.
	// The owner's server puts the mark on them (the frame comes back like any remote edit).
	function startComment() {
		const c = ctx;
		if (!c || !cancomment) return;
		const sel = c.view.getSelection();
		const picked = sel ? commentSelection(c.doc, sel.anchor, sel.focus) : null;
		if (!picked) {
			say('Select the text to comment on first');
			return;
		}
		oncompose?.({ text: picked.text, first: picked.first, last: picked.last });
	}

	let flashTimer: ReturnType<typeof setTimeout> | null = null;
	let flashed: HTMLElement[] = [];

	/** Scroll to comment `id`'s mark and flash it. False when no character carries it. */
	export function goTo(id: string): boolean {
		const c = ctx;
		if (!c || !commentRange(c.doc, id)) return false;
		const marks = [...c.root.querySelectorAll<HTMLElement>('mark.ne-comment')].filter((m) =>
			(m.dataset.comment ?? '').split(' ').includes(id)
		);
		if (!marks.length) return false;
		marks[0].scrollIntoView?.({ block: 'center', behavior: 'smooth' });
		for (const m of flashed) m.classList.remove('ne-comment-flash');
		flashed = marks;
		for (const m of marks) m.classList.add('ne-comment-flash');
		if (flashTimer) clearTimeout(flashTimer);
		flashTimer = setTimeout(() => {
			for (const m of flashed) m.classList.remove('ne-comment-flash');
			flashed = [];
		}, 1800);
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
		const { view, doc, undo } = ctx;
		const sel = view.getSelection();
		const at = sel ? Math.min(sel.anchor, sel.focus) : Math.max(0, doc.web.textLen(doc.obj) - 1);
		const line = view.lines.length ? lineAt(view.lines as Line[], at) : null;
		if (line?.cells) return; // already in a table: Tab adds rows
		view.beforeChange();
		const r = insertTable(doc, at, 2, 2);
		undo.push(r.inverse, { caret: at });
		guest.localChange();
		view.update();
		view.setSelection(r.caret);
		canUndo = undo.canUndo();
		canRedo = undo.canRedo();
	}

	function linkButton() {
		if (!focusEditor() || !ctx) return;
		// Ctrl+K is the editor's own link command; ask it the same way.
		linkReq = null;
		ctx.root.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true, bubbles: true, cancelable: true }));
		if (!linkReq) say('Select the text to link first');
	}

	function undoButton() {
		if (focusEditor()) ctx?.input.undo();
	}
	function redoButton() {
		if (focusEditor()) ctx?.input.redo();
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
			say('That kind of link is not allowed');
			return;
		}
		if (!/^[a-z][a-z0-9+.-]*:/i.test(url)) url = 'https://' + url;
		closeLink(url);
	}

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
		{ kind: 'task', label: '☐', name: 'Task list', keys: 'Ctrl+Shift+9' }
	] as const;
	const marks = [
		{ kind: 'bold', label: 'B', name: 'Bold', keys: 'Ctrl+B', cls: 'font-bold' },
		{ kind: 'italic', label: 'I', name: 'Italic', keys: 'Ctrl+I', cls: 'italic' },
		{ kind: 'strike', label: 'S', name: 'Strikethrough', keys: 'Ctrl+Shift+X', cls: 'line-through' },
		{ kind: 'code', label: '</>', name: 'Inline code', keys: 'Ctrl+E', cls: 'font-mono' }
	] as const;
</script>

<div class="space-y-2">
	{#if !readOnly}
		<div class="flex flex-wrap gap-1.5" role="toolbar" aria-label="Format notes">
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
				<button
					type="button"
					onmousedown={(e) => e.preventDefault()}
					onclick={undoButton}
					disabled={!canUndo}
					aria-label="Undo"
					title="Undo your last edit (Ctrl+Z)"
					class="{btn(false)} disabled:opacity-40 disabled:hover:text-gray-400">↶</button
				>
				<button
					type="button"
					onmousedown={(e) => e.preventDefault()}
					onclick={redoButton}
					disabled={!canRedo}
					aria-label="Redo"
					title="Redo (Ctrl+Shift+Z)"
					class="{btn(false)} disabled:opacity-40 disabled:hover:text-gray-400">↷</button
				>
			</div>
			{#if cancomment && selected}
				<div class={GROUP}>
					<button
						type="button"
						onmousedown={(e) => e.preventDefault()}
						onclick={startComment}
						aria-label="Comment on the selection"
						title="Comment on the selected text (Ctrl+Alt+M)"
						class={btn(false)}>💬 Comment</button
					>
				</div>
			{/if}
		</div>
	{/if}

	<p class="min-h-4 text-[11px] text-amber-400" aria-live="polite">{notice}</p>

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

	<div
		bind:this={boxEl}
		class="min-h-64 px-4 py-3 bg-gray-900 border border-gray-700 rounded-lg focus-within:border-gray-500"
	>
		<div bind:this={rootEl} class="min-h-[14.5rem]" aria-label="Meeting notes" data-testid="guest-notes-editor"></div>
	</div>
</div>

<style>
	:global(.notes-editor mark.ne-comment.ne-comment-flash) {
		background: rgb(250 204 21 / 0.55);
		transition: background 0.4s;
	}
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
