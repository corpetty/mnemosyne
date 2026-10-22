// The editor view: mounts the DOM layer (dom.ts) on a document and keeps the selection across
// re-renders by element identity. Ported by name from hashseq's web/kb.js (MIT, David Rusu):
// captureEditState / restoreEditState (the id of the character left of each selection end via
// seqIdAt, mapped back with seqPositionOf: ids never move, offsets do; APP_NOTES.md 20) and
// remoteRender's deferral during an IME composition (`deferUpdates`).
//
// Input handling (beforeinput, shortcuts, paste, IME, undo) is the next layer; it drives this
// one through `update`, `invalidate`, `deferUpdates` and the selection helpers.
import { cpLen, lines, type Doc, type Line } from '../model.js';
import {
  ROOT_CLASS,
  getSelectionOffsets,
  invalidateLine,
  lineElementOf,
  lineIdOf,
  locatePoint,
  offsetOfPoint,
  render,
  setSelectionOffsets,
  type DomPoint,
  type SelectionOffsets,
  type ToggleTaskHandler
} from './dom.js';

export interface EditState {
  /** Element id of the character left of the anchor; null = start of doc. */
  anchor: string | null;
  focus: string | null;
  /** The offsets at capture time: the fallback when an id was deleted. */
  anchorOffset: number;
  focusOffset: number;
  scrollTop: number;
}

/**
 * The selection inside `root` as element ids (or null for "start of doc") plus scroll
 * position; null when the selection is elsewhere.
 */
export function captureEditState(doc: Doc, root: HTMLElement, scroller: HTMLElement = root): EditState | null {
  const sel = getSelectionOffsets(root);
  if (!sel) return null;
  const idBefore = (off: number): string | null => (off > 0 ? (doc.web.seqIdAt(doc.obj, off - 1) ?? null) : null);
  return {
    anchor: idBefore(sel.anchor),
    focus: idBefore(sel.focus),
    anchorOffset: sel.anchor,
    focusOffset: sel.focus,
    scrollTop: scroller.scrollTop
  };
}

/**
 * Where a captured end is now: after its character, or its old offset clamped to the last
 * caret position (the end of the last line) when that character was deleted.
 */
export function restoredOffset(doc: Doc, id: string | null, oldOffset: number): number {
  if (id === null) return 0;
  const p = doc.web.seqPositionOf(doc.obj, id);
  if (p !== undefined) return p + 1;
  return Math.max(0, Math.min(oldOffset, cpLen(doc.web.text(doc.obj)) - 1));
}

/** Put the selection back on the same characters after a re-render, and the scroll position. */
export function restoreEditState(
  doc: Doc,
  root: HTMLElement,
  state: EditState,
  opts: { preferAfter?: boolean; scroller?: HTMLElement } = {}
): SelectionOffsets {
  const anchor = restoredOffset(doc, state.anchor, state.anchorOffset);
  const focus = restoredOffset(doc, state.focus, state.focusOffset);
  const active = root.ownerDocument.activeElement;
  if (active !== root && !root.contains(active)) root.focus({ preventScroll: true });
  setSelectionOffsets(root, anchor, focus, opts.preferAfter ?? false);
  (opts.scroller ?? root).scrollTop = state.scrollTop;
  return { anchor, focus };
}

export interface EditorViewOptions {
  root: HTMLElement;
  doc: Doc;
  readOnly?: boolean;
  onToggleTask?: ToggleTaskHandler;
  /** The element whose scrollTop the edit state keeps (default: the root). */
  scroller?: HTMLElement;
}

export interface EditorView {
  readonly root: HTMLElement;
  /** The lines as of the last `update`. */
  readonly lines: readonly Line[];
  /**
   * Record the selection by element id before the doc changes under the DOM (a remote frame
   * about to be applied): the next `update` restores from it. Offsets the DOM shows are only
   * valid against the doc text it was rendered from, so this must run BEFORE `applyRemote`.
   * Does nothing when the root is not focused or a capture is already pending.
   */
  beforeChange(): void;
  /**
   * Re-read `lines(doc)`, render incrementally, restore the selection if the root had focus:
   * from the pending `beforeChange` capture, else (a local edit whose DOM already shows the
   * doc's text) from the selection as it is now.
   */
  update(opts?: { preferAfter?: boolean }): void;
  /** While true (an IME composition), updates are queued: one runs when it is cleared. */
  deferUpdates: boolean;
  readOnly: boolean;
  /** Force the next update to rebuild this line's element (after a native edit to it). */
  invalidate(lineId: string | null): void;
  getSelection(): SelectionOffsets | null;
  setSelection(anchor: number, focus?: number, preferAfter?: boolean): void;
  offsetOf(node: Node, offset: number): number | null;
  pointAt(offset: number, preferAfter?: boolean): DomPoint;
  /** The line element containing a node and its line id. */
  lineOf(node: Node | null): { el: HTMLElement; id: string | null } | null;
  capture(): EditState | null;
  restore(state: EditState, preferAfter?: boolean): SelectionOffsets;
  destroy(): void;
}

export function createEditorView(options: EditorViewOptions): EditorView {
  const { root, doc } = options;
  const scroller = options.scroller ?? root;
  let current: Line[] = [];
  let deferred = false;
  let pending = false;
  let captured: EditState | null | undefined; // undefined = nothing captured
  let readOnly = options.readOnly ?? false;

  root.classList.add(ROOT_CLASS);
  root.setAttribute('role', 'textbox');
  root.setAttribute('aria-multiline', 'true');
  const applyReadOnly = () => {
    root.contentEditable = readOnly ? 'false' : 'true';
    root.setAttribute('aria-readonly', readOnly ? 'true' : 'false');
  };
  applyReadOnly();

  const checkboxOf = (target: EventTarget | null): HTMLElement | null =>
    target instanceof Element ? (target.closest('.ne-check') as HTMLElement | null) : null;
  // A click on the checkbox toggles the task; mousedown is swallowed so it neither moves the
  // selection nor takes focus from the text.
  const onMouseDown = (e: MouseEvent) => {
    if (checkboxOf(e.target)) e.preventDefault();
  };
  const onClick = (e: MouseEvent) => {
    const box = checkboxOf(e.target);
    if (!box || readOnly) return;
    e.preventDefault();
    const lineEl = lineElementOf(root, box);
    if (!lineEl) return;
    const id = lineIdOf(lineEl);
    const index = current.findIndex((l) => l.id === id);
    if (index >= 0) options.onToggleTask?.(index);
  };
  root.addEventListener('mousedown', onMouseDown);
  root.addEventListener('click', onClick);

  const hasFocus = (): boolean => {
    const active = root.ownerDocument.activeElement;
    return active === root || root.contains(active);
  };

  const view: EditorView = {
    root,
    get lines() {
      return current;
    },
    get deferUpdates() {
      return deferred;
    },
    set deferUpdates(v: boolean) {
      deferred = v;
      if (!v && pending) {
        pending = false;
        view.update();
      }
    },
    get readOnly() {
      return readOnly;
    },
    set readOnly(v: boolean) {
      readOnly = v;
      applyReadOnly();
    },
    beforeChange() {
      if (captured !== undefined) return;
      captured = hasFocus() ? captureEditState(doc, root, scroller) : null;
    },
    update(opts = {}) {
      if (deferred) {
        pending = true;
        return;
      }
      const state = captured !== undefined ? captured : hasFocus() ? captureEditState(doc, root, scroller) : null;
      captured = undefined;
      current = lines(doc);
      render(root, current);
      if (state) restoreEditState(doc, root, state, { preferAfter: opts.preferAfter, scroller });
    },
    invalidate: (id) => invalidateLine(root, id),
    getSelection: () => getSelectionOffsets(root),
    setSelection: (anchor, focus = anchor, preferAfter = false) => setSelectionOffsets(root, anchor, focus, preferAfter),
    offsetOf: (node, offset) => offsetOfPoint(root, node, offset),
    pointAt: (offset, preferAfter = false) => locatePoint(root, offset, preferAfter),
    lineOf(node) {
      const el = lineElementOf(root, node);
      return el ? { el, id: lineIdOf(el) } : null;
    },
    capture: () => captureEditState(doc, root, scroller),
    restore: (state, preferAfter = false) => restoreEditState(doc, root, state, { preferAfter, scroller }),
    destroy() {
      root.removeEventListener('mousedown', onMouseDown);
      root.removeEventListener('click', onClick);
    }
  };
  return view;
}
