<script lang="ts">
	// The notes editor as both the desktop (components/SharedNotes.svelte) and the guest page
	// (guest/src/Notes.svelte) mount it: the view, input layer and local undo on the host's document,
	// the first edit's terminating newline, remote changes that keep the caret (`bindRemote`),
	// comment marks painted from the open threads, the link prompt, Ctrl+Alt+M, the slim toolbar, the
	// selection toolbar floating over a selection and our selection for live carets. Page-specific behaviour comes in through `host` (ui/types.ts);
	// nothing here imports outside src/lib/notes, so the guest Vite entry bundles it.
	// On touch screens (coarse pointer) the touch bar is the toolbar (no slim toolbar, no hint
	// line), its buttons act on the last selection the editor had, Comment opens a sheet above the
	// keyboard (CommentSheet.svelte, when the host can post: `postComment`), and the line being
	// typed is kept above the bar.
	// Images (host.media): pasted, dropped or picked (`/Image`) files are prepared one at a time
	// (prepareImage.ts), each goes in at once as an image line with its final mark (one undo entry)
	// and is uploaded beside it; an observer fills every image line's <img> from the provider and
	// sets its state; a ready picture opens full size in the lightbox (click, Enter or Space).
	import { onMount, tick, untrack, type Snippet } from 'svelte';
	import { parseChip } from '../chips.js';
	import { commentRange, commentSelection, type CommentSelection } from '../comments.js';
	import { insertImageLine, removeImageLine, toggleTask } from '../commands.js';
	import { imageValue } from '../images.js';
	import { ImageRefused, prepareImage, UNREADABLE, type PreparedImage } from '../prepareImage.js';
	import { PASTE_LIMIT } from '../editor/clipboard.js';
	import '../editor/editor.css';
	import { attachInput, type EditorInput, type LinkRequest } from '../editor/input.js';
	import { caretRect } from '../editor/rects.js';
	import { createEditorView, restoredOffset, type EditState, type EditorView } from '../editor/view.js';
	import { ensureTerminated, lineAt, lines, spansOf, valuesAt, type Doc, type Line, type LineKind } from '../model.js';
	import { UndoStack } from '../undo.js';
	import CaretOverlay from './CaretOverlay.svelte';
	import { leafRects, placeToolbar, type Rect } from './placement.js';
	import SelectionToolbar, { type SelectionActive, type SelectionMark } from './SelectionToolbar.svelte';
	import SlimToolbar from './SlimToolbar.svelte';
	import TouchBar from './TouchBar.svelte';
	import CommentSheet from './CommentSheet.svelte';
	import { caretScroll, isCoarse, scrollParent, watchCoarse } from './viewport.js';
	import SlashMenu from './SlashMenu.svelte';
	import Lightbox from './Lightbox.svelte';
	import type { MediaProvider, NotesHost, SelectionIds, ToolbarActive } from './types.js';

	let {
		host,
		testid,
		label,
		boxClass = '',
		ready = $bindable(false),
		beside,
		under,
		above
	}: {
		host: NotesHost;
		/** data-testid of the editable root. */
		testid: string;
		/** aria-label of the editable root. */
		label?: string;
		/** Extra classes on the editor's box (a height limit that makes it scroll). */
		boxClass?: string;
		/** True while the editor is mounted on a document. */
		ready?: boolean;
		/** Beside the toolbar, on its right (the status line). */
		beside?: Snippet;
		/** Under the toolbar row (a notice line). */
		under?: Snippet;
		/** Right above the editor's box (a comment composer). */
		above?: Snippet;
	} = $props();

	const PLACEHOLDER = 'Notes about this meeting… # heading, - list, [ ] task, **bold**';
	const HINT = 'Type / for blocks and inserts, @ for a person, [[ for a meeting';

	const doc = $derived(host.doc);
	const readOnly = $derived(host.readOnly);
	const canComment = $derived(host.canComment);
	const media = $derived(host.media);

	let rootEl = $state<HTMLElement>();
	let boxEl = $state<HTMLElement>();
	let input = $state.raw<EditorInput | null>(null);
	/** Everything the handlers below need from the mounted editor (not reactive). */
	interface Mounted {
		doc: Doc;
		view: EditorView;
		input: EditorInput;
		undo: UndoStack;
		root: HTMLElement;
		paint: () => void;
	}
	let ctx: Mounted | null = null;
	let canUndo = $state(false);
	let canRedo = $state(false);

	// ---- the editor: mounted once the document exists, again if its identity changes ---------------
	$effect(() => {
		const d = doc;
		const root = rootEl;
		if (!d || !root) return;
		return untrack(() => mount(d, root));
	});

	function mount(doc: Doc, root: HTMLElement): () => void {
		const undo = new UndoStack(doc);
		const refreshEmpty = () => root.toggleAttribute('data-empty', doc.web.textLen(doc.obj) <= 1);
		const refreshUndo = () => {
			canUndo = undo.canUndo();
			canRedo = undo.canRedo();
		};
		const changed = () => {
			host.localChange();
			refreshEmpty();
			refreshUndo();
			schedule();
			recheckSlash();
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
			onTyping: () => {
					dismissBar();
					host.setTyping(true);
				},
			requestLink: openLink,
			onTyped: (char, caret) => slash?.typed(char, caret),
			interceptKey: (e) => slash?.key(e) ?? false,
			onFiles: (files, from) => (ctx ? takeFiles(files, whereAt(doc, from), ctx) : false),
			onPasteTooLarge: (n) =>
				host.say(
					`That paste is too long for the notes (${n.toLocaleString('en-US')} characters; the limit is ${PASTE_LIMIT.toLocaleString('en-US')})`,
					'error'
				)
		});
		const unbind = host.bindRemote({
			wrap: (apply) => inp.applyRemote(apply),
			after: () => {
				refreshEmpty();
				refreshUndo();
				schedule();
				recheckSlash();
			}
		});
		root.dataset.placeholder = PLACEHOLDER;
		const onBlur = () => {
			host.flush();
			sendSel(null);
		};
		root.addEventListener('focusout', onBlur);
		// Open threads' marks are highlighted, resolved and orphaned ones are plain text. The
		// editor renders the marks without knowing the threads, so the class is put on afterwards,
		// after every render (the observer) and every change of the open threads (the effect below).
		const paint = () => {
			const open = host.openComments;
			for (const m of root.querySelectorAll<HTMLElement>('mark.ne-comment')) {
				const on = (m.dataset.comment ?? '').split(' ').some((id) => open.has(id));
				if (m.classList.contains('ne-comment-open') !== on) m.classList.toggle('ne-comment-open', on);
			}
		};
		const painter = new MutationObserver(paint);
		painter.observe(root, { childList: true, subtree: true });
		// Image lines: a rendered (or rebuilt) line starts at `loading` without a src; fill it. Our
		// own changes inside a picture (its note and buttons) are not renders.
		const imager = new MutationObserver((records) => {
			if (records.some((r) => !(r.target instanceof Element && r.target.closest('figure.ne-image')))) fillImages();
		});
		imager.observe(root, { childList: true, subtree: true });
		// A mousedown on a picture keeps the caret where it is (the click opens it or runs a button).
		const onImageDown = (e: MouseEvent) => {
			if (e.target instanceof Element && e.target.closest('figure.ne-image')) e.preventDefault();
		};
		root.addEventListener('mousedown', onImageDown);
		// A focused picture is a button: Enter or Space opens it; no key reaches the input layer
		// (the text selection may be anywhere meanwhile). Tab moves on as usual.
		const onImageKey = (e: KeyboardEvent) => {
			const fig = e.target instanceof HTMLElement && e.target.matches('figure.ne-image') ? e.target : null;
			if (!fig) return;
			e.stopPropagation();
			if ((e.key === 'Enter' || e.key === ' ') && !e.ctrlKey && !e.metaKey && !e.altKey) {
				e.preventDefault();
				openLightbox(fig);
			}
		};
		root.addEventListener('keydown', onImageKey, true);
		// A click on a highlight (not the end of a drag) opens its threads; a click on a chip goes
		// to the host's InsertProvider (also when read-only; a host without onChipClick: nothing).
		const onClick = (e: MouseEvent) => {
			const target = e.target instanceof Element ? e.target : null;
			const fig = target?.closest<HTMLElement>('figure.ne-image');
			if (fig && root.contains(fig)) {
				e.preventDefault();
				const act = target?.closest<HTMLElement>('button[data-act]')?.dataset.act;
				if (act === 'retry' || act === 'remove') imageAction(fig, act);
				else openLightbox(fig);
				return;
			}
			const sel = document.getSelection();
			if (sel && !sel.isCollapsed) return;
			const chipEl = target?.closest<HTMLElement>('a.ne-chip[data-chip]');
			if (chipEl && root.contains(chipEl)) {
				const chip = parseChip(chipEl.dataset.chip ?? '');
				if (chip && host.inserts?.onChipClick) {
					e.preventDefault();
					host.inserts.onChipClick(chip);
					return;
				}
			}
			const mark = target?.closest<HTMLElement>('mark.ne-comment-open');
			if (!mark || !root.contains(mark)) return;
			const open = host.openComments;
			const ids = (mark.dataset.comment ?? '').split(' ').filter((id) => open.has(id));
			if (ids.length) host.onMark(ids);
		};
		root.addEventListener('click', onClick);
		view.update();
		refreshEmpty();
		refreshUndo();
		ctx = { doc, view, input: inp, undo, root, paint };
		lastSel = null;
		paint();
		fillImages();
		input = inp;
		ready = true;
		schedule();
		return () => {
			slash?.close();
			root.removeEventListener('focusout', onBlur);
			root.removeEventListener('click', onClick);
			painter.disconnect();
			imager.disconnect();
			root.removeEventListener('mousedown', onImageDown);
			root.removeEventListener('keydown', onImageKey, true);
			lightbox = null;
			root.removeEventListener('beforeinput', onBeforeInput);
			host.flush();
			host.setTyping(false);
			sendSel(null);
			unbind();
			inp.detach();
			view.destroy();
			root.replaceChildren();
			root.removeAttribute('data-empty');
			if (ctx?.view === view) ctx = null;
			input = null;
			ready = false;
			linkReq = null;
			lastSel = null;
			sheet = null;
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
		void host.openComments;
		ctx?.paint();
	});

	// ---- after a render or a selection change: the toolbar's state and our caret -------------------
	const NONE: ToolbarActive = { kind: null, bold: false, italic: false };
	let active = $state<ToolbarActive>(NONE);
	/** The same for the selection toolbar (the selection's start). */
	let selActive = $state<SelectionActive>({ ...NONE, strike: false, code: false, link: false });
	/** Is there text selected in the editor? (enables the Comment button) */
	let selected = $state(false);
	let frame = 0;

	function schedule() {
		if (frame) return;
		frame = requestAnimationFrame(() => {
			frame = 0;
			if (!readOnly) computeToolbar();
			selNow();
			updateBar();
			slash?.reposition();
			keepCaretVisible();
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
		const next: ToolbarActive = {
			kind: kind === 'task_done' ? 'task' : kind,
			bold: has('bold'),
			italic: has('italic')
		};
		if (JSON.stringify(next) !== JSON.stringify(active)) active = next;
		const nextSel: SelectionActive = {
			...next,
			strike: has('strike'),
			code: has('code'),
			link: has('link')
		};
		if (JSON.stringify(nextSel) !== JSON.stringify(selActive)) selActive = nextSel;
	}

	/** What was last given to `presence.setSel` ("a|f"), null = no selection. */
	let sentSel: string | null = null;

	function sendSel(sel: SelectionIds | null) {
		const p = host.presence;
		if (!p) return;
		const key = sel ? `${sel.a ?? ''}|${sel.f ?? ''}` : null;
		if (key === sentSel) return;
		sentSel = key;
		p.setSel(sel);
	}

	/** Our selection as element ids (captureEditState's), while it is in the editor. */
	function selNow() {
		const c = ctx;
		if (!c || !host.presence) return;
		const s = c.view.capture();
		// A read-only editor has no focus of its own: its selection counts wherever focus is.
		const here = s && (c.view.readOnly || c.root.contains(document.activeElement));
		sendSel(here ? { a: s.anchor, f: s.focus } : null);
	}

	onMount(() => {
		const onSelection = () => {
			const root = rootEl;
			const sel = document.getSelection();
			const node = sel?.anchorNode;
			if (root && node && root.contains(node)) {
				selected = !sel.isCollapsed;
				remember();
				slash?.recheck();
				schedule();
			} else if (sentSel !== null) schedule();
		};
		document.addEventListener('selectionchange', onSelection);
		// Ctrl+Alt+M comments on the selection, also in a read-only editor (which has no focus of
		// its own to carry the key press).
		const onKey = (e: KeyboardEvent) => {
			if (barPos && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
				const inRoot = !!rootEl && !!document.activeElement && rootEl.contains(document.activeElement);
				if (e.key === 'F10' && e.altKey && (inRoot || readOnly)) {
					const node = document.getSelection()?.anchorNode;
					if (rootEl && node && rootEl.contains(node)) {
						e.preventDefault();
						savedSel = ctx?.view.getSelection() ?? null;
						selBar?.focusFirst();
					}
					return;
				}
				// Escape in the editor hides the toolbar until the selection changes; the key goes on
				// to whatever else listens (a menu, the host).
				if (e.key === 'Escape' && !e.altKey && inRoot) {
					dismissBar();
					return;
				}
			}
			if (!(e.ctrlKey && e.altKey && !e.shiftKey && !e.metaKey && e.code === 'KeyM')) return;
			const node = document.getSelection()?.anchorNode;
			if (!rootEl || !node || !rootEl.contains(node)) return;
			e.preventDefault();
			startComment();
		};
		document.addEventListener('keydown', onKey);
		// The selection toolbar follows scroll (the box's and any ancestor's) and resizing.
		const follow = () => {
			if (barPos || slash?.isOpen()) schedule();
		};
		document.addEventListener('scroll', follow, true);
		window.addEventListener('resize', follow);
		// Touch screens: the keyboard coming up hides the bottom of the page; the caret follows.
		const stopCoarse = watchCoarse((c) => (coarse = c));
		const vv = typeof window !== 'undefined' ? window.visualViewport : null;
		const keyboard = () => {
			if (coarse && touchFocus) schedule();
		};
		vv?.addEventListener('resize', keyboard);
		// Focus kept the touch bar up by moving into it (boxFocusOut): leaving the bar for anywhere
		// but the editor takes it down.
		const barFocusOut = (e: FocusEvent) => {
			if (!(e.target instanceof Node) || !touchBar?.contains(e.target)) return;
			const to = e.relatedTarget;
			if (to instanceof Node && (boxEl?.contains(to) || touchBar.contains(to))) return;
			touchFocus = false;
		};
		document.addEventListener('focusout', barFocusOut, true);
		const ro = typeof ResizeObserver === 'function' && boxEl ? new ResizeObserver(follow) : null;
		if (ro && boxEl) ro.observe(boxEl);
		return () => {
			document.removeEventListener('selectionchange', onSelection);
			document.removeEventListener('keydown', onKey);
			document.removeEventListener('scroll', follow, true);
			window.removeEventListener('resize', follow);
			stopCoarse();
			vv?.removeEventListener('resize', keyboard);
			document.removeEventListener('focusout', barFocusOut, true);
			ro?.disconnect();
			if (frame) cancelAnimationFrame(frame);
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

	/** "Turn into": set the line kind, never toggling it off (null = plain text). */
	function turnInto(kind: LineKind | null) {
		if (focusEditor()) ctx?.input.setLineKind(kind, { toggle: false });
	}
	function toggleTaskList() {
		if (focusEditor()) ctx?.input.setLineKind('task');
	}
	function toggle(kind: 'bold' | 'italic') {
		if (focusEditor()) ctx?.input.toggleInline(kind);
	}
	function undoButton() {
		if (focusEditor()) ctx?.input.undo();
	}
	function redoButton() {
		if (focusEditor()) ctx?.input.redo();
	}
	/** The slim toolbar's Insert: the `/` menu at the caret. */
	function bulletButton() {
		if (focusEditor()) ctx?.input.setLineKind('bullet');
	}
	function insertButton() {
		if (focusEditor()) slash?.openAtCaret();
	}

	// ---- the `/` menu and the `@` / `[[` pickers (SlashMenu.svelte) -----------------------------------
	let slash = $state<ReturnType<typeof SlashMenu>>();
	let slashQueued = false;
	/** After a change: once it has rendered and the caret is placed, the menu follows its query. */
	function recheckSlash() {
		if (slashQueued || !slash?.isOpen()) return;
		slashQueued = true;
		queueMicrotask(() => {
			slashQueued = false;
			slash?.recheck();
		});
	}

	// ---- the selection toolbar -----------------------------------------------------------------------
	// Shown over a non-collapsed selection in the editor (also in a read-only one that may comment),
	// positioned from the selection's client rects relative to the box (placement.ts). Hidden on a
	// collapsed selection, Escape, typing and when focus leaves the box (the toolbar lives in it, so
	// focus moving into the toolbar does not count); not on touch screens, which have their own bar.
	let selBar = $state<ReturnType<typeof SelectionToolbar>>();
	let barGeom = $state.raw<{ rects: Rect[]; box: Rect; scrollTop: number; scrollLeft: number } | null>(null);
	let barSize = $state({ w: 0, h: 0 });
	const barPos = $derived(
		barGeom
			? placeToolbar({
					rects: barGeom.rects,
					box: barGeom.box,
					scrollTop: barGeom.scrollTop,
					scrollLeft: barGeom.scrollLeft,
					toolbar: { width: barSize.w || 300, height: barSize.h || 32 }
				})
			: null
	);
	/** Does focus sit in the editor's box (the editor or the toolbar)? */
	let focusWithin = false;
	/** Reactive copy of `focusWithin` for the touch bar; `touchShown` = the bar is up. */
	let touchFocus = $state(false);
	let touchShown = $state(false);
	/** The selection the toolbar was dismissed on ("anchor:focus"): it stays away until it changes. */
	let dismissedOn: string | null = null;
	/** The editor's selection when focus went to the toolbar, put back by Escape and the buttons. */
	let savedSel: { anchor: number; focus: number } | null = null;

	const sigOf = (s: { anchor: number; focus: number } | null) => (s ? `${s.anchor}:${s.focus}` : '');

	function dismissBar() {
		if (!barPos) return;
		dismissedOn = sigOf(ctx?.view.getSelection() ?? null);
		barGeom = null;
	}

	/** Re-measure the toolbar after it rendered (its width depends on the mode). */
	$effect(() => {
		void barGeom;
		void readOnly;
		void canComment;
		const el = selBar?.element();
		if (!el) return;
		const w = el.offsetWidth;
		const h = el.offsetHeight;
		if (untrack(() => barSize.w !== w || barSize.h !== h)) barSize = { w, h };
	});

	function updateBar() {
		const hide = () => {
			if (barGeom) barGeom = null;
		};
		const c = ctx;
		const box = boxEl;
		const root = rootEl;
		if (!c || !box || !root) return hide();
		if (typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches) return hide();
		if (readOnly && !canComment) return hide();
		const sel = document.getSelection();
		const inRoot =
			!!sel &&
			sel.rangeCount > 0 &&
			!sel.isCollapsed &&
			!!sel.anchorNode &&
			!!sel.focusNode &&
			root.contains(sel.anchorNode) &&
			root.contains(sel.focusNode);
		// Focus in the toolbar: keep it until the selection collapses or the toolbar loses focus.
		if (!inRoot) return selBar?.hasFocus() && barGeom ? undefined : hide();
		if (!readOnly && !focusWithin) return hide();
		const here = sigOf(c.view.getSelection());
		if (dismissedOn !== null) {
			if (dismissedOn === here) return hide();
			dismissedOn = null;
		}
		const range = sel!.getRangeAt(0);
		const all = range.getClientRects ? [...range.getClientRects()] : [];
		let rects: Rect[] = all.map((r) => ({ left: r.left, top: r.top, right: r.right, bottom: r.bottom }));
		if (!rects.length && range.getBoundingClientRect) {
			const r = range.getBoundingClientRect();
			rects = [{ left: r.left, top: r.top, right: r.right, bottom: r.bottom }];
		}
		rects = leafRects(rects);
		if (!rects.length) return hide();
		const b = box.getBoundingClientRect();
		const left = b.left + box.clientLeft;
		const top = b.top + box.clientTop;
		barGeom = {
			rects,
			box: { left, top, right: left + box.clientWidth, bottom: top + box.clientHeight },
			scrollTop: box.scrollTop,
			scrollLeft: box.scrollLeft
		};
	}

	function boxFocusIn(e: FocusEvent) {
		focusWithin = true;
		touchFocus = true;
		// Focus moving into the toolbar (Tab, Alt+F10): remember where the editor's selection was.
		if (selBar?.element()?.contains(e.target as Node)) savedSel ??= ctx?.view.getSelection() ?? null;
		else savedSel = null;
		schedule();
	}

	function boxFocusOut(e: FocusEvent) {
		const to = e.relatedTarget;
		if (to instanceof Node && boxEl?.contains(to)) return;
		focusWithin = false;
		// A browser that still moves focus to a touch bar button: the bar stays for its action,
		// which gives the editor its focus back.
		if (!(to instanceof Node && touchBar?.contains(to))) touchFocus = false;
		savedSel = null;
		slash?.close();
		schedule();
	}

	/** Give the editor its focus and selection back (from the toolbar). */
	function backToEditor() {
		const c = ctx;
		if (!c) return;
		const keep = savedSel ?? c.view.getSelection();
		savedSel = null;
		if (!c.view.readOnly) c.root.focus({ preventScroll: true });
		if (keep) c.view.setSelection(keep.anchor, keep.focus);
	}

	/** Escape in the toolbar: back to the editor, toolbar away until the selection changes. */
	function barEscape() {
		const keep = savedSel ?? ctx?.view.getSelection() ?? null;
		backToEditor();
		dismissedOn = sigOf(keep);
		barGeom = null;
	}

	/** Run a toolbar action on the selection; from the toolbar's own focus the editor is refocused first. */
	function onSelection(act: () => void) {
		if (selBar?.hasFocus()) backToEditor();
		act();
	}

	function barKind(kind: LineKind | null) {
		onSelection(() => ctx?.input.setLineKind(kind, { toggle: false }));
	}
	function barToggle(mark: SelectionMark) {
		onSelection(() => ctx?.input.toggleInline(mark));
	}
	function barLink() {
		onSelection(() => ctx?.input.requestLink());
	}
	function barComment() {
		onSelection(startComment);
	}

	// ---- comments ----------------------------------------------------------------------------------
	// A comment is made from the selection: its text and the ids of its first and last character.
	// The server marks them (the frame comes back like any remote edit), so a reader may comment.
	function startComment() {
		const c = ctx;
		if (!c || !canComment) return;
		const sel = c.view.getSelection();
		const picked = sel ? commentSelection(c.doc, sel.anchor, sel.focus) : null;
		if (!picked) {
			host.say('Select the text to comment on first', 'info');
			return;
		}
		if (coarse && host.postComment) openSheet(picked);
		else host.compose(picked);
	}

	// ---- touch screens -------------------------------------------------------------------------------
	// A tap on the touch bar must not cost the editor its selection, but a browser may still blur
	// the editor or clear the selection on the way (see TouchBar.svelte). So the editor remembers
	// its last selection as element ids (they survive remote edits, view.capture) whenever it
	// changes, and the bar's actions run on that one when the live selection is gone.
	let coarse = $state(isCoarse());
	/** The editor's last selection while it was in the editor (selectionchange). */
	let lastSel: EditState | null = null;
	let touchBar = $state<ReturnType<typeof TouchBar>>();
	/** The touch bar's height while it shows (the caret is kept above it). */
	let touchHeight = $state(0);
	/** The selection the comment sheet is writing on (null: no sheet). */
	let sheet = $state.raw<CommentSelection | null>(null);
	/** The editor's selection when the sheet opened, put back by Cancel. */
	let sheetSel: EditState | null = null;

	function remember() {
		const s = ctx?.view.capture();
		if (s) lastSel = s;
	}

	/** The live selection in the editor, else the remembered one (offsets now). */
	function touchSelection(): { anchor: number; focus: number } | null {
		const c = ctx;
		if (!c) return null;
		const live = c.view.getSelection();
		if (live) return live;
		return lastSel ? offsetsOf(lastSel) : null;
	}

	function offsetsOf(s: EditState): { anchor: number; focus: number } | null {
		const c = ctx;
		if (!c) return null;
		return {
			anchor: restoredOffset(c.doc, s.anchor, s.anchorOffset),
			focus: restoredOffset(c.doc, s.focus, s.focusOffset)
		};
	}

	/** Give the editor its focus and the remembered selection back when it lost them. */
	function restoreTouch(s: EditState | null = lastSel): boolean {
		const c = ctx;
		if (!c) return false;
		const focused = c.view.readOnly || c.root.contains(document.activeElement);
		if (focused && c.view.getSelection()) return true;
		const at = s ? offsetsOf(s) : null;
		if (!at) return false;
		if (!c.view.readOnly) c.root.focus({ preventScroll: true });
		c.view.setSelection(at.anchor, at.focus);
		return true;
	}

	/** A touch bar button: on the editor's selection, put back first when the tap took it. */
	function touchRun(act: () => void) {
		restoreTouch();
		act();
	}

	function touchComment() {
		const c = ctx;
		if (!c || !canComment) return;
		const sel = touchSelection();
		const picked = sel ? commentSelection(c.doc, sel.anchor, sel.focus) : null;
		if (!picked) {
			host.say('Select the text to comment on first', 'info');
			return;
		}
		if (host.postComment) openSheet(picked);
		else {
			restoreTouch();
			host.compose(picked);
		}
	}

	function openSheet(picked: CommentSelection) {
		remember();
		sheetSel = lastSel;
		sheet = picked;
		// Focus goes to the sheet's text box (a bar button holding it is removed with the bar, which
		// fires no focusout): the editor is not being typed in any more.
		touchFocus = false;
	}

	async function sheetSubmit(text: string) {
		const s = sheet;
		if (!s || !host.postComment) return;
		try {
			await host.postComment(s, text);
		} catch (e) {
			host.say(e instanceof Error ? e.message : 'Could not post the comment', 'error');
			throw e;
		}
		if (sheet === s) {
			sheet = null;
			sheetSel = null;
		}
	}

	/** Cancel or Escape: the sheet goes, the editor's selection comes back. */
	function sheetCancel() {
		const keep = sheetSel;
		sheet = null;
		sheetSel = null;
		restoreTouch(keep);
	}

	/**
	 * Touch screens, while typing: keep the caret above the touch bar (and below the visible top),
	 * scrolling the page or the scrolling ancestor. Only a caret: a selection being dragged by its
	 * handles is left alone.
	 */
	function keepCaretVisible() {
		const c = ctx;
		const box = boxEl;
		if (!c || !box || !coarse || readOnly || !touchFocus || sheet) return;
		const sel = c.view.getSelection();
		if (!sel || sel.anchor !== sel.focus) return;
		const r = caretRect(c.root, sel.focus, { left: 0, top: 0 });
		if (!r || !(r.h > 0)) return;
		const vv = window.visualViewport;
		let top = vv ? vv.offsetTop : 0;
		let bottom = top + (vv ? vv.height : window.innerHeight);
		let bar = touchHeight;
		const sp = scrollParent(box);
		const page = !sp || sp === document.scrollingElement;
		if (sp && !page) {
			// An inner scroller: its own edges count, and the bar only where it covers it.
			const b = sp.getBoundingClientRect();
			bar = Math.max(0, bar - Math.max(0, bottom - b.bottom));
			top = Math.max(top, b.top);
			bottom = Math.min(bottom, b.bottom);
		}
		const dy = caretScroll({ top: r.y, bottom: r.y + r.h, viewTop: top, viewHeight: bottom - top, bar });
		if (Math.abs(dy) < 1) return;
		if (sp && !page) sp.scrollBy?.(0, dy);
		else window.scrollBy?.(0, dy);
	}

	/**
	 * Select comment `id`'s range and scroll to it ("Go to" in the app's Comments card). The mark
	 * in the document, else `fallback` (where the last fold saw it), clamped.
	 */
	export function selectComment(id: string, fallback: number[] | null = null): void {
		const c = ctx;
		if (!c) return;
		let range = commentRange(c.doc, id);
		if (!range && fallback) {
			const len = Math.max(0, c.doc.web.textLen(c.doc.obj) - 1);
			range = { start: Math.min(fallback[0], len), end: Math.min(fallback[1], len) };
		}
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

	let flashTimer: ReturnType<typeof setTimeout> | null = null;
	let flashed: HTMLElement[] = [];

	/** Scroll to comment `id`'s mark and flash it (the guest page). False when no character carries it. */
	export function flashComment(id: string): boolean {
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

	// ---- images ------------------------------------------------------------------------------------
	/**
	 * Where the next image goes, by element id so typing or remote edits meanwhile do not move it:
	 * the position after element `prev` (null = the document's start), or after the image line whose
	 * terminator is `line` (the one inserted before it, so several files keep their order).
	 */
	type Where = { prev: string | null } | { line: string };

	function whereAt(d: Doc, pos: number): Where {
		return { prev: pos > 0 ? (d.web.seqIdAt(d.obj, pos - 1) ?? null) : null };
	}

	function resolveWhere(c: Mounted, w: Where): number {
		const d = c.doc;
		if ('line' in w) {
			const p = d.web.seqPositionOf(d.obj, w.line);
			if (p !== undefined) return p;
		} else if (w.prev === null) return 0;
		else {
			const p = d.web.seqPositionOf(d.obj, w.prev);
			if (p !== undefined) return p + 1;
		}
		return c.view.getSelection()?.focus ?? Math.max(0, d.web.textLen(d.obj) - 1);
	}

	/** Prepared images whose upload has not succeeded yet: Retry sends them again. */
	const kept = new Map<string, PreparedImage>();
	/** Upload states seen by this editor, for a provider without `state`. */
	const uploads = new Map<string, 'uploading' | 'failed'>();
	/** Images are prepared one at a time, across pastes too. */
	let imageQueue: Promise<void> = Promise.resolve();
	const watchedImgs = new WeakSet<HTMLImageElement>();
	const noteKeys = new WeakMap<HTMLElement, string>();

	/** Take the image files among `files` (false: none, or no media; the text path runs). */
	function takeFiles(files: File[], where: Where, c: Mounted): boolean {
		if (!host.media || c.input.readOnly) return false;
		const images = files.filter((f) => f.type.startsWith('image/'));
		if (!images.length) return false;
		imageQueue = imageQueue.then(() => addImages(images, where, c));
		return true;
	}

	async function addImages(files: File[], where: Where, c: Mounted) {
		for (const f of files) {
			let p: PreparedImage;
			try {
				p = await prepareImage(f);
			} catch (e) {
				host.say(e instanceof ImageRefused ? e.message : UNREADABLE, 'error');
				continue;
			}
			const m = host.media;
			// The editor went to another document, or became read-only, meanwhile.
			if (ctx !== c || !m || c.input.readOnly) return;
			const value = imageValue(p);
			const r = c.input.command((d) => insertImageLine(d, resolveWhere(c, where), value));
			if (!r || !r.inverse.length) continue;
			const id = lineAt(lines(c.doc), r.caret).id;
			if (id) where = { line: id };
			upload(m, p);
		}
	}

	/** Upload (again), never blocking the line that names it. */
	function upload(m: MediaProvider, p: PreparedImage) {
		kept.set(p.sha, p);
		uploads.set(p.sha, 'uploading');
		refreshImage(p.sha);
		let done: Promise<void>;
		try {
			done = Promise.resolve(m.upload(p));
		} catch (e) {
			done = Promise.reject(e);
		}
		done.then(
			() => {
				kept.delete(p.sha);
				uploads.delete(p.sha);
				refreshImage(p.sha);
			},
			() => {
				uploads.set(p.sha, 'failed');
				refreshImage(p.sha);
			}
		);
	}

	/** The provider's upload state first, then ours; "missing" only when no upload of ours is pending or failed. */
	function imageState(m: MediaProvider | undefined, sha: string): 'uploading' | 'failed' | 'missing' | null {
		const theirs = m?.state?.(sha) ?? null;
		if (theirs === 'uploading' || theirs === 'failed') return theirs;
		return uploads.get(sha) ?? theirs;
	}

	/** Give one picture its src, state, note and buttons from the provider (idempotent). */
	function fillImage(fig: HTMLElement) {
		const sha = fig.dataset.sha ?? '';
		const m = host.media;
		const img = fig.querySelector('img');
		const url = m ? m.url(sha) : null;
		if (img) {
			if (url) {
				if (img.getAttribute('src') !== url) img.src = url;
			} else if (img.hasAttribute('src')) img.removeAttribute('src');
			if (!watchedImgs.has(img)) {
				watchedImgs.add(img);
				img.addEventListener('load', () => fillImage(fig));
				img.addEventListener('error', () => fillImage(fig));
			}
		}
		const shown = !!url && !!img && img.getAttribute('src') === url && img.complete;
		const loaded = shown && img!.naturalWidth > 0;
		const broken = shown && !loaded;
		const st = imageState(m, sha);
		const state =
			st === 'failed'
				? 'failed'
				: st === 'uploading'
					? 'uploading'
					: loaded
						? 'ready'
						: st === 'missing' || broken
							? 'missing'
							: 'loading';
		if (fig.dataset.state !== state) fig.dataset.state = state;
		if (state === 'ready') {
			if (fig.getAttribute('tabindex') !== '0') fig.setAttribute('tabindex', '0');
			if (fig.getAttribute('aria-label') !== 'Open image') fig.setAttribute('aria-label', 'Open image');
		} else {
			if (fig.hasAttribute('tabindex')) fig.removeAttribute('tabindex');
			if (fig.hasAttribute('aria-label')) fig.removeAttribute('aria-label');
		}
		const ro = ctx?.input.readOnly ?? readOnly;
		const retry = state === 'failed' && !ro && kept.has(sha) && !!m;
		const remove = state === 'failed' && !ro;
		const key = `${state}|${retry}|${remove}`;
		if (noteKeys.get(fig) === key) return;
		noteKeys.set(fig, key);
		const note = fig.querySelector<HTMLElement>('.ne-image-note');
		if (!note) return;
		const text =
			state === 'uploading' ? 'Uploading…' : state === 'failed' ? 'Not uploaded' : state === 'missing' ? 'Image not available' : '';
		const parts: Node[] = text ? [document.createTextNode(text)] : [];
		const button = (act: string, label: string) => {
			const b = document.createElement('button');
			b.type = 'button';
			b.dataset.act = act;
			b.textContent = label;
			parts.push(b);
		};
		if (retry) button('retry', 'Retry');
		if (remove) button('remove', 'Remove');
		note.replaceChildren(...parts);
	}

	function fillImages() {
		const root = ctx?.root;
		if (!root) return;
		for (const fig of root.querySelectorAll<HTMLElement>('figure.ne-image')) fillImage(fig);
	}

	function refreshImage(sha: string) {
		const root = ctx?.root;
		if (!root || !/^[0-9a-f]+$/.test(sha)) return;
		for (const fig of root.querySelectorAll<HTMLElement>(`figure.ne-image[data-sha="${sha}"]`)) fillImage(fig);
	}

	// The provider says when bytes arrive or an upload changes; a new provider or a change of the
	// read-only flag (the buttons) refills every picture.
	$effect(() => {
		const m = media;
		if (!input) return;
		untrack(fillImages);
		return m?.subscribe((sha) => refreshImage(sha));
	});
	$effect(() => {
		void readOnly;
		if (input) untrack(fillImages);
	});

	/** The image line a picture belongs to, as it is in the document now. */
	function lineOfFigure(fig: HTMLElement): Line | null {
		const c = ctx;
		const ref = c?.view.lineOf(fig);
		if (!c || !ref || ref.id === null) return null;
		return lines(c.doc).find((l) => l.id === ref.id) ?? null;
	}

	function imageAction(fig: HTMLElement, act: 'retry' | 'remove') {
		const c = ctx;
		if (!c || c.input.readOnly) return;
		if (act === 'retry') {
			const p = kept.get(fig.dataset.sha ?? '');
			const m = host.media;
			if (p && m) upload(m, p);
			return;
		}
		const line = lineOfFigure(fig);
		if (line?.image) c.input.command((d) => removeImageLine(d, line));
	}

	// `/Image`: the host's picker, else the editor's own file input (on a phone it offers the camera).
	let fileInput = $state<HTMLInputElement>();
	let pickWhere: Where | null = null;

	function pickImage() {
		const c = ctx;
		const m = host.media;
		if (!c || !m || c.input.readOnly) return;
		const at = c.view.getSelection()?.focus ?? Math.max(0, c.doc.web.textLen(c.doc.obj) - 1);
		const where = whereAt(c.doc, at);
		if (m.pick) {
			void m.pick().then(
				(f) => {
					if (f && ctx === c) picked([f], where, c);
				},
				() => {}
			);
			return;
		}
		pickWhere = where;
		fileInput?.click();
	}

	function onFileChosen(e: Event) {
		const el = e.currentTarget as HTMLInputElement;
		const files = Array.from(el.files ?? []);
		el.value = '';
		const where = pickWhere;
		pickWhere = null;
		const c = ctx;
		if (files.length && where && c) picked(files, where, c);
	}

	/** A file chosen in a picker that is no image the browser names as one: said, not ignored. */
	function picked(files: File[], where: Where, c: Mounted) {
		if (!takeFiles(files, where, c)) host.say(UNREADABLE, 'error');
	}

	// ---- the lightbox --------------------------------------------------------------------------------
	let lightbox = $state.raw<{ src: string; label: string; fig: HTMLElement; sha: string } | null>(null);

	function openLightbox(fig: HTMLElement) {
		if (fig.dataset.state !== 'ready') return;
		const src = fig.querySelector('img')?.getAttribute('src');
		if (!src) return;
		const caption = lineOfFigure(fig)?.text.trim() ?? '';
		lightbox = { src, label: caption || 'Image', fig, sha: fig.dataset.sha ?? '' };
	}

	/** Close it and give the picture its focus back (the same image's, if its line was rebuilt meanwhile). */
	function closeLightbox() {
		const lb = lightbox;
		lightbox = null;
		if (!lb) return;
		let fig: HTMLElement | null = lb.fig;
		if (!fig.isConnected && /^[0-9a-f]+$/.test(lb.sha))
			fig = ctx?.root.querySelector<HTMLElement>(`figure.ne-image[data-sha="${lb.sha}"]`) ?? null;
		fig?.focus({ preventScroll: true });
	}

	// ---- the link prompt (Ctrl+K) ------------------------------------------------------------------
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
			host.say('That kind of link is not allowed', 'error');
			return;
		}
		if (!/^[a-z][a-z0-9+.-]*:/i.test(url)) url = 'https://' + url;
		closeLink(url);
	}
</script>

<div class="space-y-2">
	{#snippet toolbar()}
		<SlimToolbar
			{active}
			{readOnly}
			{canComment}
			{selected}
			commentActive={host.commentActive ?? false}
			hideCommentUntilSelected={host.hideCommentUntilSelected ?? false}
			hideComment={(!!barPos || touchShown) && canComment}
			undoButtons={host.undoButtons}
			{canUndo}
			{canRedo}
			onkind={turnInto}
			ontoggle={toggle}
			ontask={toggleTaskList}
			oncomment={startComment}
			onundo={undoButton}
			onredo={redoButton}
			oninsert={insertButton}
		/>
	{/snippet}
	{#if beside}
		<div class="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
			{#if (!readOnly || canComment) && !coarse}
				{@render toolbar()}
			{:else}
				<span></span>
			{/if}
			{@render beside()}
		</div>
	{:else if !coarse}
		{@render toolbar()}
	{/if}

	{@render under?.()}

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

	{@render above?.()}

	<div
		bind:this={boxEl}
		onfocusin={boxFocusIn}
		onfocusout={boxFocusOut}
		onscroll={() => {
			if (barPos || slash?.isOpen()) schedule();
		}}
		class="relative min-h-64 px-4 py-3 bg-gray-900 border border-gray-700 rounded-lg focus-within:border-gray-500 {boxClass}"
	>
		<div bind:this={rootEl} class="min-h-[14.5rem]" aria-label={label} data-testid={testid}></div>
		<CaretOverlay presence={host.presence} {doc} root={rootEl} />
		<SlashMenu bind:this={slash} editor={() => ctx} box={boxEl} inserts={host.inserts} image={!!media && !readOnly} onimage={pickImage} />
		{#if barPos}
			<SelectionToolbar
				bind:this={selBar}
				active={selActive}
				{readOnly}
				{canComment}
				commentActive={host.commentActive ?? false}
				top={barPos.top}
				left={barPos.left}
				side={barPos.side}
				onkind={barKind}
				ontoggle={barToggle}
				onlink={barLink}
				oncomment={barComment}
				onescape={barEscape}
			/>
		{/if}
	</div>
	<TouchBar
		bind:this={touchBar}
		{active}
		visible={!sheet && (touchFocus || (readOnly && selected))}
		{readOnly}
		{canComment}
		bind:shown={touchShown}
		bind:height={touchHeight}
		ontoggle={(kind) => touchRun(() => toggle(kind))}
		onbullet={() => touchRun(bulletButton)}
		ontask={() => touchRun(toggleTaskList)}
		onslash={() => touchRun(insertButton)}
		oncomment={touchComment}
	/>
	{#if sheet}
		<CommentSheet quote={sheet.text} onsubmit={sheetSubmit} oncancel={sheetCancel} />
	{/if}
	{#if !readOnly && !coarse}
		<p class="text-[11px] text-gray-600">{HINT}</p>
	{/if}
	{#if media && !readOnly}
		<input
			bind:this={fileInput}
			type="file"
			accept="image/*"
			capture="environment"
			hidden
			tabindex="-1"
			aria-hidden="true"
			data-testid="notes-image-input"
			onchange={onFileChosen}
		/>
	{/if}
	{#if lightbox}
		<Lightbox src={lightbox.src} label={lightbox.label} onclose={closeLightbox} />
	{/if}
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
