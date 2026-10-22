// The input layer of the notes editor (phase 6 design, docs/plans/2026-10-12-hashseq-
// collaboration.md): keyboard, mouse, clipboard and IME input over the DOM layer (view.ts),
// turned into commands on the shared document (commands.ts) with local undo (undo.ts).
//
// Ported by name from hashseq's web/kb.js (MIT, David Rusu): plain typing is left to the
// browser and committed by a caret-aware diff of the edited line (native.ts), the browser's
// placement of a boundary keystroke decides mark affinity, composition defers renders,
// input rules run after the typed delimiter is in the seq (commands.ts), structural edits
// are `beforeinput` commands.
//
// What is native and what is a command:
// - insertText, deleteContentBackward/Forward, deleteWord*, deleteSoftLine*, spellcheck
//   replacements inside ONE line and cell: the browser edits the DOM, `input` commits the
//   diff. Anything crossing a line terminator or a cell separator (a selection over lines,
//   Backspace at a line start, Delete at a line end, a word deletion reaching the line start)
//   is prevented and done as a command.
// - Enter (insertParagraph and insertLineBreak alike): `splitLine`, after the Enter input
//   rules (a ``` fence, "|a|b|" → table). Backspace at a line start: `joinWithPrevious`
//   (clears the kind first, joins second); in a table row it removes an empty row and does
//   nothing on a filled one. Delete at a line end joins the next line (never into or out of a
//   table). In a table row these and the cell steps run on keydown: WebKit fires no
//   beforeinput at a cell's edge.
// - Cut copies the selection's markdown first; paste reads text/plain as markdown.
//
// Shortcuts (Ctrl is also Cmd):
//   Ctrl+B / Ctrl+I / Ctrl+E / Ctrl+Shift+X   bold / italic / code / strikethrough on the selection
//   Ctrl+K                                      link: `requestLink` with the selection (or the
//                                               link under the caret); the component answers with `setLink`
//   Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y              undo / redo / redo
//   Ctrl+Alt+1 / 2 / 3, Ctrl+Alt+0              heading 1 / 2 / 3 (again: plain), plain
//   Ctrl+Shift+7 / 8 / 9                        numbered / bullet / task line (again: plain)
//   Tab / Shift+Tab                             in a table: next / previous cell (Tab in the last
//                                               cell adds a row); elsewhere the browser's (focus)
//   ArrowLeft / ArrowRight (+Shift)             one code point, skipping the DOM's dead stops
//                                               (fillers, the checkbox island); the caret attaches
//                                               to the character it crossed, so moving right to the
//                                               end of a bold word continues bold, moving left to
//                                               its start does not
// Undo: typed characters group until a space or a pause (undo.ts); every command is its
// own entry; an input rule is an entry after the typed text, so undo right after a rule
// reverts the conversion and leaves the typed characters.
import {
  applyInputRule,
  deleteRange,
  inputRule,
  insertModel,
  insertText,
  joinWithPrevious,
  nextCell,
  prevCell,
  removeRow,
  setLineKind,
  setLink as setLinkCommand,
  splitLine,
  toggleInline as toggleInlineCommand,
  type CommandResult
} from '../commands.js';
import { CELL_SEP, cpLen, cpSlice, ensureTerminated, lineAt, lines, spansOf, valuesAt, type Doc, type InlineKind, type Line, type LineKind } from '../model.js';
import { idsOf, liveRuns, removeRange, type Inverse } from '../ops.js';
import type { UndoStack } from '../undo.js';
import { PASTE_LIMIT, cellPasteText, cleanPaste, parsePaste, selectionHtml, selectionMarkdown } from './clipboard.js';
import { extractText, type SelectionOffsets } from './dom.js';
import { diffAt, domFlagsAt, lineContent, reconcileAffinity } from './native.js';
import type { EditorView } from './view.js';

export interface LinkRequest {
  from: number;
  to: number;
  /** The URL already on the range, if any. */
  url: string | null;
}

export interface InputOptions {
  undo: UndoStack;
  /** After every local change to the document (the sync store drains and sends). */
  onLocalChange?: () => void;
  /** On every edit intent (presence). */
  onTyping?: () => void;
  readOnly?: boolean;
  /** Ctrl+K: show a link prompt for the range; answer with `setLink`. */
  requestLink?: (range: LinkRequest) => void;
  /** A paste over `pasteLimit` code points was refused. */
  onPasteTooLarge?: (chars: number) => void;
  pasteLimit?: number;
}

export type ToggleKind = Exclude<InlineKind, 'link'>;

interface LineSnapshot {
  lineId: string | null;
  text: string;
  /** Element id of every character, so an edit against this text maps into the doc as it is now. */
  ids: string[];
}

export interface EditorInput {
  detach(): void;
  setReadOnly(v: boolean): void;
  readonly readOnly: boolean;
  /** True during an IME composition. */
  readonly composing: boolean;
  /** Apply a remote frame: the selection is kept by element id and no undo entry is made. */
  applyRemote(apply: () => void): void;
  /** Set or clear (null) the link over a range (the answer to `requestLink`). */
  setLink(from: number, to: number, url: string | null): void;
  /** Toggle an inline mark over the selection (the toolbar's buttons). */
  toggleInline(kind: ToggleKind): void;
  /** Set the kind of the selected lines (null = plain), toggling off when they already have it. */
  setLineKind(kind: LineKind | null): void;
  undo(): void;
  redo(): void;
}

/** inputTypes the browser performs itself when they stay inside one line and cell. */
const NATIVE_INSERTS = new Set(['insertText', 'insertReplacementText', 'insertTranspose']);
const BACKWARD_DELETES = new Set(['deleteContentBackward', 'deleteWordBackward', 'deleteSoftLineBackward', 'deleteHardLineBackward']);
const FORWARD_DELETES = new Set(['deleteContentForward', 'deleteWordForward', 'deleteSoftLineForward', 'deleteHardLineForward']);
const OTHER_NATIVE_DELETES = new Set(['deleteContent', 'deleteEntireSoftLine']);

const HEADINGS: Record<string, LineKind | null> = { Digit0: null, Digit1: 'h1', Digit2: 'h2', Digit3: 'h3' };
const LISTS: Record<string, LineKind> = { Digit7: 'number', Digit8: 'bullet', Digit9: 'task' };

export function attachInput(view: EditorView, doc: Doc, opts: InputOptions): EditorInput {
  const root = view.root;
  const { undo } = opts;
  const domDoc = root.ownerDocument;
  let readOnly = opts.readOnly ?? view.readOnly;
  view.readOnly = readOnly;
  let composing = false;
  /** The selection when the edit began: the caret undo restores. */
  let selBefore: SelectionOffsets | null = null;
  const pasteLimit = opts.pasteLimit ?? PASTE_LIMIT;

  const ordered = (s: SelectionOffsets): [number, number] => [Math.min(s.anchor, s.focus), Math.max(s.anchor, s.focus)];
  const selection = (): [number, number] | null => {
    const s = view.getSelection();
    return s ? ordered(s) : null;
  };
  const lastCaret = () => Math.max(0, doc.web.textLen(doc.obj) - 1);
  const changed = () => opts.onLocalChange?.();

  /** Run a command on the current doc, record it, re-render and place the caret. */
  const run = (
    fn: () => CommandResult,
    place: { caret?: number; focus?: number; preferAfter?: boolean } = {}
  ): CommandResult => {
    const before = view.getSelection();
    view.beforeChange();
    const r = fn();
    undo.push(r.inverse, { caret: before?.focus ?? r.caret });
    if (r.inverse.length) changed();
    view.update();
    const caret = place.caret ?? r.caret;
    view.setSelection(caret, place.focus ?? caret, place.preferAfter ?? false);
    return r;
  };

  /** Line elements in the DOM (a placeholder <br> inside one is not a line, a cloned div is). */
  const domLineCount = (): number => {
    let n = 0;
    for (const child of Array.from(root.children)) n += child.tagName === 'TABLE' ? child.querySelectorAll('tr').length : 1;
    return n;
  };

  const resync = () => {
    for (const l of view.lines) view.invalidate(l.id);
    view.invalidate(null);
    view.update();
  };

  const crossesCell = (line: Line, from: number, to: number): boolean =>
    line.cells !== null && cpSlice(line.text, from - line.start, to - line.start).includes(CELL_SEP);
  const spansLines = (ls: Line[], from: number, to: number): boolean => {
    const a = lineAt(ls, from);
    return a.index !== lineAt(ls, to).index || crossesCell(a, from, to);
  };

  // ---- commands ------------------------------------------------------------------------

  const deleteSelection = (from: number, to: number) => run(() => deleteRange(doc, from, to));

  /** Replace `[from, to)` with plain text; "\n" splits lines (never inside a table row). */
  const insertPlainText = (from: number, to: number, data: string) =>
    run(() => {
      const inv: Inverse[] = [];
      if (from < to) inv.push(...deleteRange(doc, from, to).inverse);
      let at = from;
      const inTable = lineAt(lines(doc), at).cells !== null;
      const parts = (inTable ? cellPasteText(data) : data).split('\n');
      parts.forEach((part, i) => {
        if (i > 0) {
          const r = splitLine(doc, at);
          inv.push(...r.inverse);
          at = r.caret;
        }
        if (part) {
          const r = insertText(doc, at, part);
          inv.push(...r.inverse);
          at = r.caret;
        }
      });
      return { inverse: inv, caret: at };
    });

  const enter = (from: number, to: number) =>
    run(() => {
      const inv: Inverse[] = [];
      if (from < to) inv.push(...deleteRange(doc, from, to).inverse);
      const ls = lines(doc);
      const line = lineAt(ls, from);
      if (from === line.end && !line.cells) {
        const rule = inputRule(line.text, from - line.start, '\n', line.kind);
        if (rule) {
          const r = applyInputRule(doc, line, rule);
          return { inverse: [...inv, ...r.inverse], caret: r.caret };
        }
      }
      const r = splitLine(doc, from);
      return { inverse: [...inv, ...r.inverse], caret: r.caret };
    });

  const backspaceAtLineStart = (line: Line) => {
    if (line.cells) {
      if (line.text.replaceAll(CELL_SEP, '') === '') run(() => removeRow(doc, line.start));
      return;
    }
    run(() => joinWithPrevious(doc, line.start));
  };

  const deleteAtLineEnd = (ls: Line[], line: Line) => {
    const next = ls[line.index + 1];
    if (!next || line.cells || next.cells) return;
    run(() => deleteRange(doc, line.end, line.end + 1));
  };

  /**
   * A collapsed Backspace (`back`) or Delete at `pos` that would leave its line or cell: done
   * here (join, remove an empty row, step to the neighbouring cell); false when it stays
   * inside one line and cell and is the browser's to do. The caller prevents the default.
   */
  const deleteAtBoundary = (back: boolean, ls: Line[], line: Line, pos: number): boolean => {
    if (back) {
      if (pos === line.start) {
        backspaceAtLineStart(line);
        return true;
      }
      if (cellBoundary(line, pos) === 'start') {
        view.setSelection(prevCell(doc, pos).end);
        return true;
      }
      return false;
    }
    if (pos === line.end) {
      deleteAtLineEnd(ls, line);
      return true;
    }
    if (cellBoundary(line, pos) === 'end') {
      const r = nextCell(doc, pos);
      if (!r.inverse.length) view.setSelection(r.caret);
      return true;
    }
    return false;
  };

  /** Backspace / Delete at a table cell's edge on keydown: WebKit fires no beforeinput there
   *  (it never deletes across cells), so the keystroke would do nothing at all. */
  const tableDeleteKey = (e: KeyboardEvent) => {
    if (e.shiftKey && e.key === 'Delete') return; // cut, on some platforms
    const s = view.getSelection();
    if (!s || s.anchor !== s.focus) return;
    const ls = lines(doc);
    const line = lineAt(ls, s.focus);
    if (!line.cells) return;
    selBefore = s;
    if (deleteAtBoundary(e.key === 'Backspace', ls, line, s.focus)) {
      e.preventDefault();
      opts.onTyping?.();
    }
  };

  const cellBoundary = (line: Line, pos: number): 'start' | 'end' | null => {
    if (!line.cells) return null;
    const i = pos - line.start;
    const cps = Array.from(line.text);
    if (i > 0 && cps[i - 1] === CELL_SEP) return 'start';
    if (i < cps.length && cps[i] === CELL_SEP) return 'end';
    return null;
  };

  /** The range a delete* will remove, when the engine says (getTargetRanges) and it leaves the line. */
  const crossingTarget = (e: InputEvent, ls: Line[], line: Line): [number, number] | null => {
    const ranges = typeof e.getTargetRanges === 'function' ? e.getTargetRanges() : [];
    for (const r of ranges) {
      const a = view.offsetOf(r.startContainer, r.startOffset);
      const b = view.offsetOf(r.endContainer, r.endOffset);
      if (a === null || b === null) continue;
      const [from, to] = [Math.min(a, b), Math.max(a, b)];
      if (lineAt(ls, from).index !== line.index || lineAt(ls, to).index !== line.index || crossesCell(line, from, to)) return [from, to];
    }
    return null;
  };

  const paste = (raw: string, from: number, to: number) => {
    const text = cleanPaste(raw);
    if (!text) return;
    const n = cpLen(text);
    if (n > pasteLimit) {
      opts.onPasteTooLarge?.(n);
      return;
    }
    run(() => {
      const inv: Inverse[] = [];
      if (from < to) inv.push(...deleteRange(doc, from, to).inverse);
      const at = from;
      const ls = lines(doc);
      const line = lineAt(ls, at);
      const done = (r: CommandResult, caret = r.caret): CommandResult => ({ inverse: [...inv, ...r.inverse], caret });
      if (line.cells) return done(insertText(doc, at, cellPasteText(text)));
      const plan = parsePaste(text);
      if (plan.kind === 'inline') return done(insertModel(doc, at, plan.model));
      if (line.start === line.end) {
        // An empty host line is replaced: the pasted lines take its place, its own
        // terminator goes (a raw removal, so the following line keeps its kind).
        const r = insertModel(doc, at, plan.model);
        inv.push(...r.inverse);
        inv.push(removeRange(doc, r.caret, r.caret + 1));
        if (ensureTerminated(doc)) {
          const id = doc.web.seqIdAt(doc.obj, doc.web.textLen(doc.obj) - 1);
          if (id) inv.push({ op: 'removeIds', ids: [id] });
        }
        return { inverse: inv, caret: r.caret - 1 };
      }
      if (at === line.start) return done(insertModel(doc, at, plan.model), cpLen(plan.model.text) + at - 1);
      if (at === line.end) {
        const r = insertModel(doc, line.end + 1, plan.model);
        return done(r, r.caret - 1);
      }
      const s = splitLine(doc, at);
      inv.push(...s.inverse);
      const r = insertModel(doc, s.caret, plan.model);
      return done(r, r.caret - 1);
    });
  };

  const doUndo = () => {
    if (!undo.canUndo()) return;
    view.beforeChange();
    const caret = undo.undo();
    changed();
    view.update();
    if (caret !== null) view.setSelection(Math.min(caret, lastCaret()));
  };

  const doRedo = () => {
    if (!undo.canRedo()) return;
    view.beforeChange();
    const caret = undo.redo();
    changed();
    view.update();
    if (caret !== null) view.setSelection(Math.min(caret, lastCaret()));
  };

  const toggleInline = (kind: ToggleKind) => {
    const s = selection();
    if (!s || s[0] === s[1]) return;
    run(() => toggleInlineCommand(doc, s[0], s[1], kind), { caret: s[0], focus: s[1] });
  };

  const setKind = (kind: LineKind | null) => {
    const s = selection();
    if (!s) return;
    const ls = lines(doc);
    const first = lineAt(ls, s[0]);
    if (first.cells) return;
    const same = first.kind === kind || (kind === 'task' && first.kind === 'task_done');
    run(() => setLineKind(doc, s[0], s[1], same ? null : kind), { caret: s[0], focus: s[1] });
  };

  const requestLink = () => {
    const s = selection();
    if (!s) return;
    let [from, to] = s;
    const spans = spansOf(doc);
    let url: string | null = valuesAt(spans, from, 'link')[0] ?? null;
    if (from === to) {
      // The link under the caret (the character before it, else after).
      url = valuesAt(spans, Math.max(0, from - 1), 'link')[0] ?? valuesAt(spans, from, 'link')[0] ?? null;
      if (!url) return;
      const has = (i: number) => valuesAt(spans, i, 'link').includes(url as string);
      from = has(from) ? from : from - 1;
      to = from + 1;
      while (from > 0 && has(from - 1)) from--;
      while (to < doc.web.textLen(doc.obj) && has(to)) to++;
    }
    opts.requestLink?.({ from, to, url });
  };

  const tab = (e: KeyboardEvent) => {
    const s = selection();
    if (!s) return;
    const line = lineAt(lines(doc), s[1]);
    if (!line.cells) return;
    e.preventDefault();
    view.beforeChange();
    const r = e.shiftKey ? prevCell(doc, s[0]) : nextCell(doc, s[1]);
    undo.push(r.inverse, { caret: s[1] });
    if (r.inverse.length) changed();
    view.update();
    view.setSelection(r.caret, r.end);
  };

  const arrow = (e: KeyboardEvent) => {
    const s = view.getSelection();
    if (!s) return;
    e.preventDefault();
    const right = e.key === 'ArrowRight';
    let focus: number;
    if (!e.shiftKey && s.anchor !== s.focus) focus = right ? Math.max(s.anchor, s.focus) : Math.min(s.anchor, s.focus);
    else focus = Math.max(0, Math.min(lastCaret(), s.focus + (right ? 1 : -1)));
    view.setSelection(e.shiftKey ? s.anchor : focus, focus, !right);
  };

  // ---- native typing -------------------------------------------------------------------

  /**
   * Commit what the browser did to the line under the caret; returns the model caret after
   * the committed text. `typed` is the keystroke's data (undo grouping, input rules).
   */
  const commitNative = (typed: string | null, base: LineSnapshot | null = null): number | null => {
    const sel = domDoc.getSelection();
    const focus = sel && sel.rangeCount > 0 && sel.focusNode ? { node: sel.focusNode, offset: sel.focusOffset } : null;
    const ref = focus ? view.lineOf(focus.node) : null;
    const ls = lines(doc);
    const line = ref ? ls.find((l) => l.id === ref.id) : undefined;
    // An engine that split or merged lines itself (a beforeinput it would not let us cancel:
    // execCommand, some virtual keyboards) cloned or dropped line elements; the line under the
    // caret is no guide then, the whole text is.
    if (!ref || !line || domLineCount() !== ls.length) return commitWhole();
    const content = lineContent(ref.el, focus);
    const snap = base && base.lineId === line.id ? base : null;
    const d = diffAt(snap ? snap.text : line.text, content.text, content.caret);
    if (!d) {
      if (extractText(root) !== doc.web.text(doc.obj)) resync();
      return null;
    }
    const inv: Inverse[] = [];
    let at: number;
    if (snap) {
      // The DOM was rendered from the snapshot (a composition deferred updates): the edit is
      // mapped into the current doc by element ids, so remote text that arrived meanwhile,
      // even in this line, is kept.
      if (d.kind !== 'insert') {
        for (const [a, b] of liveRuns(doc, snap.ids.slice(d.p, d.p + d.n)).reverse()) inv.push(removeRange(doc, a, b));
      }
      const anchor = d.p > 0 ? doc.web.seqPositionOf(doc.obj, snap.ids[d.p - 1]) : undefined;
      const now = lines(doc).find((l) => l.id === line.id);
      if (!now) return commitWhole();
      at = anchor !== undefined ? anchor + 1 : now.start;
    } else {
      at = line.start + d.p;
      if (d.kind !== 'insert') inv.push(...deleteRange(doc, at, at + d.n).inverse);
    }
    let caret = at;
    if (d.kind !== 'remove') {
      const n = cpLen(d.text);
      inv.push(...insertText(doc, at, d.text).inverse);
      caret = at + n;
      if (focus && content.caret === d.p + n) inv.push(...reconcileAffinity(doc, at, n, domFlagsAt(focus.node, ref.el)));
    }
    undo.push(inv, {
      caret: selBefore?.focus ?? caret,
      typed: d.kind === 'insert' && typed !== null && d.text === typed ? typed : undefined
    });
    changed();
    view.update();
    if (d.kind === 'insert' && (d.text === ' ' || d.text === '`')) {
      const now = lines(doc);
      const l = lineAt(now, caret);
      if (!l.cells) {
        const rule = inputRule(l.text, caret - l.start, d.text, l.kind);
        if (rule) {
          view.beforeChange();
          const r = applyInputRule(doc, l, rule);
          undo.push(r.inverse, { caret });
          changed();
          view.update();
          view.setSelection(r.caret, r.caret, true);
          caret = r.caret;
        }
      }
    }
    if (extractText(root) !== doc.web.text(doc.obj)) resync();
    return caret;
  };

  /** The browser edited something we cannot place by line: diff the whole text, then rebuild. */
  const commitWhole = (): number | null => {
    const s = view.getSelection();
    const d = diffAt(doc.web.text(doc.obj), extractText(root), s?.focus ?? null);
    let caret: number | null = null;
    if (d) {
      view.beforeChange();
      const inv: Inverse[] = [];
      if (d.kind !== 'insert') inv.push(...deleteRange(doc, d.p, d.p + d.n).inverse);
      if (d.kind !== 'remove') {
        inv.push(...insertText(doc, d.p, d.text).inverse);
        caret = d.p + cpLen(d.text);
      } else caret = d.p;
      undo.push(inv, { caret: selBefore?.focus ?? caret });
      changed();
    }
    resync();
    if (caret !== null) view.setSelection(Math.min(caret, lastCaret()));
    return caret;
  };

  // ---- events --------------------------------------------------------------------------

  const onBeforeInput = (e: InputEvent) => {
    if (readOnly) {
      e.preventDefault();
      return;
    }
    const type = e.inputType;
    if (composing || e.isComposing || type === 'insertCompositionText') return;
    opts.onTyping?.();
    const s = view.getSelection();
    if (!s) {
      e.preventDefault();
      return;
    }
    selBefore = s;
    const [from, to] = ordered(s);
    const ls = lines(doc);
    const line = lineAt(ls, from);
    const multi = from < to && spansLines(ls, from, to);
    if (NATIVE_INSERTS.has(type)) {
      const data = e.data ?? e.dataTransfer?.getData('text/plain') ?? '';
      if (!multi && !data.includes('\n')) return;
      e.preventDefault();
      insertPlainText(from, to, data);
      return;
    }
    if (BACKWARD_DELETES.has(type) || FORWARD_DELETES.has(type) || OTHER_NATIVE_DELETES.has(type)) {
      if (from < to) {
        if (multi) {
          e.preventDefault();
          deleteSelection(from, to);
        }
        return;
      }
      const back = BACKWARD_DELETES.has(type);
      if ((back || FORWARD_DELETES.has(type)) && deleteAtBoundary(back, ls, line, from)) {
        e.preventDefault();
        return;
      }
      const target = type === 'deleteContentBackward' || type === 'deleteContentForward' ? null : crossingTarget(e, ls, line);
      if (target) {
        e.preventDefault();
        deleteSelection(target[0], target[1]);
      }
      return;
    }
    e.preventDefault();
    switch (type) {
      case 'insertParagraph':
      case 'insertLineBreak':
        enter(from, to);
        return;
      case 'deleteByCut':
        cut(from, to, null);
        return;
      case 'deleteByDrag':
        if (from < to) deleteSelection(from, to);
        return;
      case 'insertFromPaste':
      case 'insertFromDrop':
      case 'insertFromYank': {
        const text = e.dataTransfer?.getData('text/plain') ?? e.data ?? '';
        if (text) paste(text, from, to);
        return;
      }
      case 'historyUndo':
        doUndo();
        return;
      case 'historyRedo':
        doRedo();
        return;
      case 'formatBold':
        toggleInline('bold');
        return;
      case 'formatItalic':
        toggleInline('italic');
        return;
      case 'formatStrikeThrough':
        toggleInline('strike');
        return;
      case 'insertOrderedList':
        setKind('number');
        return;
      case 'insertUnorderedList':
        setKind('bullet');
        return;
      default:
        // insertHorizontalTab, formatUnderline, formatIndent, insertLink and the rest: nothing.
        return;
    }
  };

  const onInput = (e: Event) => {
    if (readOnly || composing || (e as InputEvent).isComposing) return;
    const type = (e as InputEvent).inputType ?? '';
    if (type.startsWith('format') || type.startsWith('insertOrdered') || type.startsWith('insertUnordered')) {
      // An engine that performed a formatting command natively (a beforeinput we could not
      // cancel): the text is unchanged, the DOM is not ours; rebuild it from the model.
      resync();
      return;
    }
    commitNative((e as InputEvent).data ?? null);
  };

  const onKeyDown = (e: KeyboardEvent) => {
    if (e.defaultPrevented) return;
    const mod = e.ctrlKey || e.metaKey;
    if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && !mod && !e.altKey) {
      if (!readOnly && !composing) arrow(e);
      return;
    }
    if (readOnly || composing) return;
    if (e.key === 'Tab' && !mod && !e.altKey) {
      tab(e);
      return;
    }
    if (e.key === 'Backspace' || e.key === 'Delete') {
      tableDeleteKey(e);
      return;
    }
    if (!mod) return;
    if (e.altKey && !e.shiftKey && e.code in HEADINGS) {
      e.preventDefault();
      setKind(HEADINGS[e.code]);
      return;
    }
    if (e.shiftKey && !e.altKey && e.code in LISTS) {
      e.preventDefault();
      setKind(LISTS[e.code]);
      return;
    }
    if (e.altKey) return;
    const k = e.key.toLowerCase();
    if (e.shiftKey) {
      if (k === 'z') doRedo();
      else if (k === 'x') toggleInline('strike');
      else return;
      e.preventDefault();
      return;
    }
    switch (k) {
      case 'b':
        toggleInline('bold');
        break;
      case 'i':
        toggleInline('italic');
        break;
      case 'e':
        toggleInline('code');
        break;
      case 'k':
        requestLink();
        break;
      case 'z':
        doUndo();
        break;
      case 'y':
        doRedo();
        break;
      default:
        return;
    }
    e.preventDefault();
  };

  const copyTo = (e: ClipboardEvent): [number, number] | null => {
    const s = selection();
    if (!s || s[0] === s[1] || !e.clipboardData) return null;
    e.clipboardData.setData('text/plain', selectionMarkdown(lines(doc), s[0], s[1]));
    e.clipboardData.setData('text/html', selectionHtml(root));
    e.preventDefault();
    return s;
  };

  const cut = (from: number, to: number, e: ClipboardEvent | null) => {
    if (e && !copyTo(e)) return;
    if (from < to) deleteSelection(from, to);
  };

  const onCopy = (e: ClipboardEvent) => {
    copyTo(e);
  };
  const onCut = (e: ClipboardEvent) => {
    if (readOnly) {
      copyTo(e);
      return;
    }
    const s = selection();
    if (!s) return;
    cut(s[0], s[1], e);
  };
  const onPaste = (e: ClipboardEvent) => {
    e.preventDefault();
    if (readOnly) return;
    const s = selection();
    if (!s) return;
    opts.onTyping?.();
    paste(e.clipboardData?.getData('text/plain') ?? '', s[0], s[1]);
  };

  /** The composed line as the DOM shows it while the IME works (updates are deferred). */
  let compositionBase: LineSnapshot | null = null;
  const onCompositionStart = () => {
    if (readOnly) return;
    composing = true;
    view.deferUpdates = true;
    undo.breakGroup();
    selBefore = view.getSelection();
    const sel = domDoc.getSelection();
    const ref = sel?.focusNode ? view.lineOf(sel.focusNode) : null;
    const line = ref ? lines(doc).find((l) => l.id === ref.id) : undefined;
    compositionBase = line ? { lineId: line.id, text: line.text, ids: idsOf(doc, line.start, line.end) } : null;
    opts.onTyping?.();
  };
  const onCompositionEnd = () => {
    if (!composing) return;
    composing = false;
    const caret = commitNative(null, compositionBase);
    compositionBase = null;
    view.deferUpdates = false;
    if (caret !== null) view.setSelection(caret);
  };

  root.addEventListener('beforeinput', onBeforeInput as EventListener);
  root.addEventListener('input', onInput);
  root.addEventListener('keydown', onKeyDown);
  root.addEventListener('copy', onCopy as EventListener);
  root.addEventListener('cut', onCut as EventListener);
  root.addEventListener('paste', onPaste as EventListener);
  root.addEventListener('compositionstart', onCompositionStart);
  root.addEventListener('compositionend', onCompositionEnd);

  return {
    get readOnly() {
      return readOnly;
    },
    get composing() {
      return composing;
    },
    detach() {
      root.removeEventListener('beforeinput', onBeforeInput as EventListener);
      root.removeEventListener('input', onInput);
      root.removeEventListener('keydown', onKeyDown);
      root.removeEventListener('copy', onCopy as EventListener);
      root.removeEventListener('cut', onCut as EventListener);
      root.removeEventListener('paste', onPaste as EventListener);
      root.removeEventListener('compositionstart', onCompositionStart);
      root.removeEventListener('compositionend', onCompositionEnd);
    },
    setReadOnly(v) {
      readOnly = v;
      view.readOnly = v;
    },
    applyRemote(apply) {
      view.beforeChange();
      apply();
      view.update();
    },
    setLink(from, to, url) {
      if (readOnly || from >= to) return;
      run(() => setLinkCommand(doc, from, to, url), { caret: from, focus: to });
    },
    toggleInline(kind) {
      if (!readOnly) toggleInline(kind);
    },
    setLineKind(kind) {
      if (!readOnly) setKind(kind);
    },
    undo() {
      if (!readOnly) doUndo();
    },
    redo() {
      if (!readOnly) doRedo();
    }
  };
}
