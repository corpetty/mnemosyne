import { describe, expect, it } from 'vitest';
import { pair, single, view as viewOf, type Client } from '../test-harness.js';
import { deleteRange, insertText, insertTable, setLineKind, setLink, splitLine, toggleInline, type NotesModel } from '../commands.js';
import { CELL_SEP, cpLen, ensureTerminated, lines, type LineKind } from '../model.js';
import { parseMarkdown } from '../pkg/notes_wasm.js';
import { applyRemote, drain } from '../sync.js';
import { UndoStack } from '../undo.js';
import { cellPasteText, parsePaste, selectionMarkdown, selectionModel } from './clipboard.js';
import { FILLER, extractText, getSelectionOffsets } from './dom.js';
import { attachInput, type EditorInput, type InputOptions, type LinkRequest } from './input.js';
import { diffAt, lineContent } from './native.js';
import { createEditorView, type EditorView } from './view.js';

// ---- fixtures ------------------------------------------------------------------------------

function docOf(...spec: string[]): Client {
  const A = single();
  ensureTerminated(A.doc);
  let at = 0;
  for (const s of spec) {
    const m = /^(.*?)(?::([a-z_0-9]+))?$/s.exec(s) as RegExpExecArray;
    const [, text, kind] = m;
    insertText(A.doc, at, text);
    splitLine(A.doc, at + cpLen(text));
    if (kind) setLineKind(A.doc, at, at, kind as LineKind);
    at += cpLen(text) + 1;
  }
  deleteRange(A.doc, at - 1, at);
  return A;
}

interface Mounted {
  A: Client;
  root: HTMLElement;
  view: EditorView;
  input: EditorInput;
  undo: UndoStack;
  clock: { t: number };
  changes: number;
  typing: number;
  links: LinkRequest[];
  tooLarge: number[];
}

function mount(A: Client, opts: { readOnly?: boolean; pasteLimit?: number; hooks?: Partial<InputOptions> } = {}): Mounted {
  const root = document.createElement('div');
  root.tabIndex = 0; // jsdom focuses a contenteditable only with a tabindex; real engines do not need it
  document.body.appendChild(root);
  const clock = { t: 0 };
  const undo = new UndoStack(A.doc, { now: () => clock.t, groupMs: 1000 });
  const view = createEditorView({ root, doc: A.doc });
  const m: Mounted = { A, root, view, undo, clock, changes: 0, typing: 0, links: [], tooLarge: [], input: null as unknown as EditorInput };
  m.input = attachInput(view, A.doc, {
    undo,
    readOnly: opts.readOnly,
    pasteLimit: opts.pasteLimit,
    onLocalChange: () => m.changes++,
    onTyping: () => m.typing++,
    requestLink: (r) => m.links.push(r),
    onPasteTooLarge: (n) => m.tooLarge.push(n),
    ...opts.hooks
  });
  view.update();
  root.focus();
  return m;
}

function beforeInput(root: HTMLElement, inputType: string, data: string | null = null, extra: Partial<InputEventInit> = {}): InputEvent {
  const e = new InputEvent('beforeinput', { inputType, data, bubbles: true, cancelable: true, ...extra });
  root.dispatchEvent(e);
  return e;
}

/** What a browser does for a native keystroke: edit the text node under the caret, then fire `input`. */
function typeNative(m: Mounted, text: string, extra: Partial<InputEventInit> = {}): void {
  const sel = document.getSelection() as Selection;
  const e = beforeInput(m.root, 'insertText', text, extra);
  if (e.defaultPrevented) return;
  let node = sel.focusNode as Node;
  let offset = sel.focusOffset;
  if (node.nodeType !== Node.TEXT_NODE) {
    // An empty line: the browser makes a text node before the sentinel.
    const t = document.createTextNode('');
    node.insertBefore(t, node.childNodes[offset] ?? null);
    node = t;
    offset = 0;
  }
  const t = node as Text;
  t.data = t.data.slice(0, offset) + text + t.data.slice(offset);
  sel.setBaseAndExtent(t, offset + text.length, t, offset + text.length);
  m.root.dispatchEvent(new InputEvent('input', { inputType: 'insertText', data: text, bubbles: true, ...extra }));
}

function backspaceNative(m: Mounted): void {
  const sel = document.getSelection() as Selection;
  const e = beforeInput(m.root, 'deleteContentBackward');
  if (e.defaultPrevented) return;
  const t = sel.focusNode as Text;
  const offset = sel.focusOffset;
  t.data = t.data.slice(0, offset - 1) + t.data.slice(offset);
  sel.setBaseAndExtent(t, offset - 1, t, offset - 1);
  m.root.dispatchEvent(new InputEvent('input', { inputType: 'deleteContentBackward', bubbles: true }));
}

function key(root: HTMLElement, key: string, init: Partial<KeyboardEventInit> = {}): KeyboardEvent {
  const e = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...init });
  root.dispatchEvent(e);
  return e;
}

/** jsdom has no ClipboardEvent: a plain event with a clipboardData the handlers can use. */
function clipboard(root: HTMLElement, type: 'copy' | 'cut' | 'paste', data: Record<string, string> = {}): { event: Event; data: Record<string, string> } {
  const store = { ...data };
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'clipboardData', {
    value: {
      getData: (k: string) => store[k] ?? '',
      setData: (k: string, v: string) => {
        store[k] = v;
      }
    }
  });
  root.dispatchEvent(event);
  return { event, data: store };
}

const sel = (m: Mounted) => getSelectionOffsets(m.root);
/** CELL_SEP as `view()` prints it (JSON-escaped). */
const SEP = '\\u001f';

// ---- native.ts ------------------------------------------------------------------------------

describe('diffAt', () => {
  it('locates an insert and a removal by the caret, in code points', () => {
    expect(diffAt('ab', 'axb', 2)).toEqual({ kind: 'insert', p: 1, text: 'x' });
    expect(diffAt('a😀b', 'a😀😀b', 2)).toEqual({ kind: 'insert', p: 1, text: '😀' });
    expect(diffAt('abc', 'ac', 1)).toEqual({ kind: 'remove', p: 1, n: 1 });
    expect(diffAt('abc', 'abc', 1)).toBeNull();
  });

  it('the caret disambiguates equal neighbours (kb APP_NOTES 17)', () => {
    // " x" typed before " x": a prefix diff would place it two characters earlier.
    expect(diffAt('a x', 'a x x', 3)).toEqual({ kind: 'insert', p: 1, text: ' x' });
    expect(diffAt('a x', 'a x x', 5)).toEqual({ kind: 'insert', p: 3, text: ' x' });
  });

  it('falls back to a prefix/suffix diff', () => {
    expect(diffAt('hello world', 'hello there', null)).toEqual({ kind: 'replace', p: 6, n: 5, text: 'there' });
    expect(diffAt('abc', 'aXc', 0)).toEqual({ kind: 'replace', p: 1, n: 1, text: 'X' });
    expect(diffAt('abc', 'abXc', 0)).toEqual({ kind: 'insert', p: 2, text: 'X' });
  });
});

describe('lineContent', () => {
  it('strips fillers, chrome and a trailing placeholder <br>; joins cells', () => {
    const el = document.createElement('div');
    el.innerHTML = `<span data-chrome="1">[]</span>${FILLER}<strong>ab</strong>${FILLER}c<br>`;
    const strong = el.querySelector('strong')?.firstChild as Text;
    expect(lineContent(el, { node: strong, offset: 2 })).toEqual({ text: 'abc', caret: 2 });
    expect(lineContent(el, { node: el, offset: el.childNodes.length })).toEqual({ text: 'abc', caret: 3 });
    el.innerHTML = 'a<br>b';
    expect(lineContent(el).text).toBe('a\nb');
    const tr = document.createElement('tr');
    tr.innerHTML = '<td>a</td><td>b</td><td data-pad="1"><br data-sentinel="1"></td>';
    expect(lineContent(tr, { node: tr.children[1].firstChild as Node, offset: 0 })).toEqual({ text: `a${CELL_SEP}b`, caret: 2 });
    // WebKit leaves a placeholder <br> in a cell it emptied: a cell is one line, not a newline.
    tr.innerHTML = '<td><br></td><td>2</td>';
    expect(lineContent(tr, { node: tr.children[0], offset: 0 })).toEqual({ text: `${CELL_SEP}2`, caret: 0 });
  });
});

// ---- typing ---------------------------------------------------------------------------------

describe('native typing', () => {
  it('commits typed characters by diff at the caret and groups them for undo', () => {
    const m = mount(docOf('hello'));
    m.view.setSelection(5);
    typeNative(m, '!');
    typeNative(m, '?');
    expect(m.A.text()).toBe('hello!?\n');
    expect(extractText(m.root)).toBe('hello!?\n');
    expect(sel(m)).toEqual({ anchor: 7, focus: 7 });
    expect(m.changes).toBe(2);
    expect(m.typing).toBe(2);
    m.undo.undo();
    expect(m.A.text()).toBe('hello\n');
  });

  it('types into an empty line and in the middle of a word', () => {
    const m = mount(docOf('', 'word'));
    m.view.setSelection(0);
    typeNative(m, 'a');
    expect(m.A.text()).toBe('a\nword\n');
    m.view.setSelection(4); // wo|rd
    typeNative(m, 'X');
    expect(m.A.text()).toBe('a\nwoXrd\n');
    expect(sel(m)).toEqual({ anchor: 5, focus: 5 });
  });

  it('commits a native backspace inside a line', () => {
    const m = mount(docOf('hello'));
    m.view.setSelection(3);
    backspaceNative(m);
    expect(m.A.text()).toBe('helo\n');
    expect(sel(m)).toEqual({ anchor: 2, focus: 2 });
    m.undo.undo();
    expect(m.A.text()).toBe('hello\n');
    expect(sel(m)).toEqual({ anchor: 2, focus: 2 });
  });

  it('follows the browser on mark affinity at a span end', () => {
    const m = mount(docOf('ab cd'));
    toggleInline(m.A.doc, 0, 2, 'bold');
    toggleInline(m.A.doc, 3, 5, 'code');
    m.view.update();
    // Inside the <strong>: the model already extends bold.
    m.view.setSelection(2, 2, false);
    typeNative(m, 'X');
    expect(m.A.view()).toBe('"abX cd":- "abX"{b} "cd"{c}');
    // In the filler after it: the browser drew it plain, so the expanding mark is cut back.
    m.view.setSelection(3, 3, true);
    typeNative(m, 'Y');
    expect(m.A.view()).toBe('"abXY cd":- "abX"{b} "cd"{c}');
    // Inside the closed <code> at its end: the closed mark is extended over it.
    m.view.setSelection(7, 7, false);
    typeNative(m, 'Z');
    expect(m.A.view()).toBe('"abXY cdZ":- "abX"{b} "cdZ"{c}');
    // And outside it (the filler) stays plain.
    m.view.setSelection(8, 8, true);
    typeNative(m, 'W');
    expect(m.A.view()).toBe('"abXY cdZW":- "abX"{b} "cdZ"{c}');
  });

  it('a selection across lines is replaced by a command', () => {
    const m = mount(docOf('one', 'two:bullet', 'three'));
    m.view.setSelection(1, 9); // o|ne\ntwo\nt|hree
    const e = beforeInput(m.root, 'insertText', 'X');
    expect(e.defaultPrevented).toBe(true);
    expect(m.A.view()).toBe('"oXhree":-');
    expect(sel(m)).toEqual({ anchor: 2, focus: 2 });
    expect(extractText(m.root)).toBe('oXhree\n');
    m.undo.undo();
    expect(m.A.view()).toBe('"one":-\n"two":bullet\n"three":-');
  });

  it('typed text with a newline becomes lines', () => {
    const m = mount(docOf('ab'));
    m.view.setSelection(1);
    beforeInput(m.root, 'insertText', 'x\ny');
    expect(m.A.text()).toBe('ax\nyb\n');
    expect(sel(m)).toEqual({ anchor: 4, focus: 4 });
  });

  it('normalises the DOM when the browser changed nothing in the text', () => {
    const m = mount(docOf('abc'));
    const el = m.root.firstElementChild as HTMLElement;
    el.innerHTML = 'abc<br>'; // an engine's placeholder
    m.view.setSelection(3);
    m.root.dispatchEvent(new InputEvent('input', { inputType: 'insertText', bubbles: true }));
    expect(m.A.text()).toBe('abc\n');
    expect(m.root.firstElementChild).not.toBe(el);
    expect(m.root.firstElementChild?.innerHTML).toBe('abc');
  });
});

describe('beforeinput commands', () => {
  it('Enter splits, continues lists and leaves an empty item', () => {
    const m = mount(docOf('item:bullet'));
    m.view.setSelection(4);
    expect(beforeInput(m.root, 'insertParagraph').defaultPrevented).toBe(true);
    expect(m.A.view()).toBe('"item":bullet\n"":bullet');
    expect(sel(m)).toEqual({ anchor: 5, focus: 5 });
    beforeInput(m.root, 'insertParagraph');
    expect(m.A.view()).toBe('"item":bullet\n"":-');
    m.view.setSelection(2);
    beforeInput(m.root, 'insertLineBreak');
    expect(m.A.view()).toBe('"it":bullet\n"em":bullet\n"":-');
    expect(sel(m)).toEqual({ anchor: 3, focus: 3 });
    m.undo.undo();
    expect(m.A.view()).toBe('"item":bullet\n"":-');
  });

  it('Enter with a selection replaces it; in a table it adds a line after the row', () => {
    const m = mount(docOf('abcd'));
    m.view.setSelection(1, 3);
    beforeInput(m.root, 'insertParagraph');
    expect(m.A.text()).toBe('a\nd\n');
    const t = mount(docOf('x'));
    insertTable(t.A.doc, 0, 2, 1);
    t.view.update();
    t.view.setSelection(0);
    beforeInput(t.root, 'insertParagraph');
    expect(t.A.text()).toBe(`${CELL_SEP}\n\nx\n`);
    expect(sel(t)).toEqual({ anchor: 2, focus: 2 });
  });

  it('Backspace at a line start clears the kind, then joins', () => {
    const m = mount(docOf('one', 'two:h2'));
    m.view.setSelection(4);
    expect(beforeInput(m.root, 'deleteContentBackward').defaultPrevented).toBe(true);
    expect(m.A.view()).toBe('"one":-\n"two":-');
    beforeInput(m.root, 'deleteContentBackward');
    expect(m.A.view()).toBe('"onetwo":-');
    expect(sel(m)).toEqual({ anchor: 3, focus: 3 });
    m.view.setSelection(0);
    beforeInput(m.root, 'deleteContentBackward'); // the first line: nothing
    expect(m.A.view()).toBe('"onetwo":-');
    // Not at a line start: native.
    m.view.setSelection(2);
    expect(beforeInput(m.root, 'deleteContentBackward').defaultPrevented).toBe(false);
  });

  it('Delete at a line end joins the next line, never a table', () => {
    const m = mount(docOf('one', 'two:bullet', 'x'));
    m.view.setSelection(3);
    expect(beforeInput(m.root, 'deleteContentForward').defaultPrevented).toBe(true);
    expect(m.A.view()).toBe('"onetwo":-\n"x":-');
    insertTable(m.A.doc, 7, 1, 1);
    m.view.update();
    m.view.setSelection(6);
    beforeInput(m.root, 'deleteContentForward');
    expect(m.A.view()).toBe('"onetwo":-\n"":table_head cells=[""]\n"x":-');
    m.view.setSelection(8); // the last line's end
    beforeInput(m.root, 'deleteContentForward');
    expect(m.A.text()).toBe(`onetwo\n\nx\n`);
  });

  it('Backspace in a table: a cell start moves back, a row start removes only an empty row', () => {
    const m = mount(docOf('x'));
    insertTable(m.A.doc, 0, 2, 2);
    insertText(m.A.doc, 0, 'a');
    m.view.update();
    m.view.setSelection(2); // second cell of the head row
    expect(beforeInput(m.root, 'deleteContentBackward').defaultPrevented).toBe(true);
    expect(sel(m)).toEqual({ anchor: 1, focus: 1 });
    m.view.setSelection(0); // row start, filled row
    beforeInput(m.root, 'deleteContentBackward');
    expect(lines(m.A.doc)).toHaveLength(3);
    m.view.setSelection(3); // the empty second row's start
    beforeInput(m.root, 'deleteContentBackward');
    expect(m.A.view()).toBe(`"a${SEP}":table_head cells=["a",""]\n"x":-`);
  });

  it('Backspace and Delete at a cell edge act on keydown (WebKit fires no beforeinput there)', () => {
    const m = mount(docOf('x'));
    insertTable(m.A.doc, 0, 2, 2);
    insertText(m.A.doc, 0, 'a');
    m.view.update();
    m.view.setSelection(2); // second cell of the head row
    expect(key(m.root, 'Backspace').defaultPrevented).toBe(true);
    expect(sel(m)).toEqual({ anchor: 1, focus: 1 });
    expect(key(m.root, 'Delete').defaultPrevented).toBe(true);
    expect(sel(m)).toEqual({ anchor: 2, focus: 2 });
    m.view.setSelection(1); // inside a cell's text: the browser's
    expect(key(m.root, 'Backspace').defaultPrevented).toBe(false);
    m.view.setSelection(3); // the empty second row's start
    expect(key(m.root, 'Backspace').defaultPrevented).toBe(true);
    expect(m.A.view()).toBe(`"a${SEP}":table_head cells=["a",""]\n"x":-`);
    m.view.setSelection(3); // the start of the line after the table: beforeinput's
    expect(key(m.root, 'Backspace').defaultPrevented).toBe(false);
  });

  it('history, format and list inputTypes route to commands', () => {
    const m = mount(docOf('hello'));
    m.view.setSelection(0, 5);
    beforeInput(m.root, 'formatBold');
    expect(m.A.view()).toBe('"hello":- "hello"{b}');
    beforeInput(m.root, 'formatItalic');
    beforeInput(m.root, 'formatStrikeThrough');
    expect(m.A.view()).toBe('"hello":- "hello"{b,i,s}');
    beforeInput(m.root, 'insertUnorderedList');
    expect(lines(m.A.doc)[0].kind).toBe('bullet');
    beforeInput(m.root, 'insertOrderedList');
    expect(lines(m.A.doc)[0].kind).toBe('number');
    beforeInput(m.root, 'historyUndo');
    expect(lines(m.A.doc)[0].kind).toBe('bullet');
    beforeInput(m.root, 'historyRedo');
    expect(lines(m.A.doc)[0].kind).toBe('number');
    expect(beforeInput(m.root, 'insertHorizontalTab').defaultPrevented).toBe(true);
    expect(m.A.text()).toBe('hello\n');
  });

  it('a spellcheck replacement inside a line is native', () => {
    const m = mount(docOf('helo'));
    m.view.setSelection(0, 4);
    expect(beforeInput(m.root, 'insertReplacementText', 'hello').defaultPrevented).toBe(false);
    const t = m.root.firstElementChild?.firstChild as Text;
    t.data = 'hello';
    m.view.setSelection(5);
    m.root.dispatchEvent(new InputEvent('input', { inputType: 'insertReplacementText', bubbles: true }));
    expect(m.A.text()).toBe('hello\n');
  });
});

describe('shortcuts', () => {
  it('Ctrl+B / I / E / Shift+X toggle inline marks over the selection', () => {
    const m = mount(docOf('hello world'));
    m.view.setSelection(0, 5);
    expect(key(m.root, 'b', { ctrlKey: true }).defaultPrevented).toBe(true);
    key(m.root, 'i', { ctrlKey: true });
    key(m.root, 'e', { ctrlKey: true });
    key(m.root, 'X', { ctrlKey: true, shiftKey: true });
    expect(m.A.view()).toBe('"hello world":- "hello"{b,i,s,c}');
    expect(sel(m)).toEqual({ anchor: 0, focus: 5 });
    key(m.root, 'b', { ctrlKey: true });
    expect(m.A.view()).toBe('"hello world":- "hello"{i,s,c}');
    // Collapsed: nothing (no pending marks in the model).
    m.view.setSelection(8);
    key(m.root, 'b', { ctrlKey: true });
    expect(m.A.view()).toBe('"hello world":- "hello"{i,s,c}');
    // Cmd works like Ctrl.
    m.view.setSelection(6, 11);
    key(m.root, 'b', { metaKey: true });
    expect(m.A.view()).toBe('"hello world":- "hello"{i,s,c} "world"{b}');
  });

  it('Ctrl+Alt+1..3 and Ctrl+Shift+7..9 set and toggle line kinds', () => {
    const m = mount(docOf('a', 'b'));
    m.view.setSelection(0, 2);
    expect(key(m.root, '1', { ctrlKey: true, altKey: true, code: 'Digit1' }).defaultPrevented).toBe(true);
    expect(lines(m.A.doc).map((l) => l.kind)).toEqual(['h1', 'h1']);
    key(m.root, '2', { ctrlKey: true, altKey: true, code: 'Digit2' });
    expect(lines(m.A.doc).map((l) => l.kind)).toEqual(['h2', 'h2']);
    key(m.root, '2', { ctrlKey: true, altKey: true, code: 'Digit2' });
    expect(lines(m.A.doc).map((l) => l.kind)).toEqual([null, null]);
    key(m.root, '3', { ctrlKey: true, altKey: true, code: 'Digit3' });
    key(m.root, '0', { ctrlKey: true, altKey: true, code: 'Digit0' });
    expect(lines(m.A.doc).map((l) => l.kind)).toEqual([null, null]);
    m.view.setSelection(0);
    key(m.root, '&', { ctrlKey: true, shiftKey: true, code: 'Digit7' });
    expect(lines(m.A.doc)[0].kind).toBe('number');
    key(m.root, '*', { ctrlKey: true, shiftKey: true, code: 'Digit8' });
    expect(lines(m.A.doc)[0].kind).toBe('bullet');
    key(m.root, '(', { ctrlKey: true, shiftKey: true, code: 'Digit9' });
    expect(lines(m.A.doc)[0].kind).toBe('task');
    key(m.root, '(', { ctrlKey: true, shiftKey: true, code: 'Digit9' });
    expect(lines(m.A.doc)[0].kind).toBeNull();
    expect(sel(m)).toEqual({ anchor: 0, focus: 0 });
  });

  it('Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y', () => {
    const m = mount(docOf('a'));
    m.view.setSelection(1);
    typeNative(m, 'b');
    expect(key(m.root, 'z', { ctrlKey: true }).defaultPrevented).toBe(true);
    expect(m.A.text()).toBe('a\n');
    expect(sel(m)).toEqual({ anchor: 1, focus: 1 });
    key(m.root, 'Z', { ctrlKey: true, shiftKey: true });
    expect(m.A.text()).toBe('ab\n');
    key(m.root, 'z', { ctrlKey: true });
    key(m.root, 'y', { ctrlKey: true });
    expect(m.A.text()).toBe('ab\n');
    expect(extractText(m.root)).toBe('ab\n');
  });

  it('Ctrl+K asks the component for a link; setLink answers', () => {
    const m = mount(docOf('see here now'));
    m.view.setSelection(4, 8);
    expect(key(m.root, 'k', { ctrlKey: true }).defaultPrevented).toBe(true);
    expect(m.links).toEqual([{ from: 4, to: 8, url: null }]);
    m.input.setLink(4, 8, 'https://x.y/');
    expect(m.A.view()).toBe('"see here now":- "here"{link=https://x.y/}');
    expect(sel(m)).toEqual({ anchor: 4, focus: 8 });
    // A collapsed caret inside the link offers the whole link.
    m.view.setSelection(6);
    key(m.root, 'k', { ctrlKey: true });
    expect(m.links[1]).toEqual({ from: 4, to: 8, url: 'https://x.y/' });
    m.view.setSelection(1);
    key(m.root, 'k', { ctrlKey: true });
    expect(m.links).toHaveLength(2);
    m.input.setLink(4, 8, null);
    expect(m.A.view()).toBe('"see here now":-');
    m.undo.undo();
    expect(m.A.view()).toBe('"see here now":- "here"{link=https://x.y/}');
  });

  it('Tab moves between cells, adds a row after the last, Shift+Tab goes back; nothing outside', () => {
    const m = mount(docOf('x'));
    insertTable(m.A.doc, 0, 2, 1);
    insertText(m.A.doc, 0, 'ab');
    insertText(m.A.doc, 3, 'c');
    m.view.update();
    m.view.setSelection(1);
    expect(key(m.root, 'Tab').defaultPrevented).toBe(true);
    expect(sel(m)).toEqual({ anchor: 3, focus: 4 });
    key(m.root, 'Tab');
    expect(m.A.view()).toBe(`"ab${SEP}c":table_head cells=["ab","c"]\n"${SEP}":table cells=["",""]\n"x":-`);
    expect(sel(m)).toEqual({ anchor: 5, focus: 5 });
    key(m.root, 'Tab', { shiftKey: true });
    expect(sel(m)).toEqual({ anchor: 3, focus: 4 });
    m.undo.undo();
    expect(lines(m.A.doc)).toHaveLength(2);
    m.view.setSelection(5);
    expect(key(m.root, 'Tab').defaultPrevented).toBe(false);
  });

  it('ArrowLeft/Right step one code point past fillers and emoji; Shift extends', () => {
    const m = mount(docOf('a😀b'));
    toggleInline(m.A.doc, 1, 2, 'bold');
    m.view.update();
    m.view.setSelection(0);
    expect(key(m.root, 'ArrowRight').defaultPrevented).toBe(true);
    expect(sel(m)).toEqual({ anchor: 1, focus: 1 });
    key(m.root, 'ArrowRight');
    expect(sel(m)).toEqual({ anchor: 2, focus: 2 });
    // Attached to the character it crossed: inside the bold span.
    expect((document.getSelection() as Selection).focusNode?.parentElement?.tagName).toBe('STRONG');
    key(m.root, 'ArrowRight');
    key(m.root, 'ArrowRight');
    expect(sel(m)).toEqual({ anchor: 3, focus: 3 }); // the line end: no further
    key(m.root, 'ArrowLeft', { shiftKey: true });
    key(m.root, 'ArrowLeft', { shiftKey: true });
    expect(sel(m)).toEqual({ anchor: 3, focus: 1 });
    key(m.root, 'ArrowLeft');
    expect(sel(m)).toEqual({ anchor: 1, focus: 1 });
    // Crossed the emoji leftwards: attached to it, inside the span (typing there is plain anyway).
    expect((document.getSelection() as Selection).focusNode?.parentElement?.tagName).toBe('STRONG');
    expect(key(m.root, 'ArrowRight', { ctrlKey: true }).defaultPrevented).toBe(false);
  });
});

describe('input rules', () => {
  it('"# " makes a heading; undo right after leaves the typed characters', () => {
    const m = mount(docOf('', 'z'));
    m.view.setSelection(0);
    typeNative(m, '#');
    typeNative(m, ' ');
    expect(m.A.view()).toBe('"":h1\n"z":-');
    expect(sel(m)).toEqual({ anchor: 0, focus: 0 });
    m.undo.undo();
    expect(m.A.view()).toBe('"# ":-\n"z":-');
    m.undo.undo(); // the space (whitespace starts its own group)
    m.undo.undo();
    expect(m.A.view()).toBe('"":-\n"z":-');
  });

  it('"- ", "1. ", "> ", "[ ] " and the code backtick', () => {
    for (const [prefix, kind] of [
      ['- ', 'bullet'],
      ['1. ', 'number'],
      ['> ', 'quote'],
      ['[ ] ', 'task']
    ] as const) {
      const m = mount(docOf(''));
      m.view.setSelection(0);
      for (const ch of prefix) typeNative(m, ch);
      expect(lines(m.A.doc)[0].kind, prefix).toBe(kind);
      expect(m.A.text()).toBe('\n');
    }
    const m = mount(docOf('use '));
    m.view.setSelection(4);
    for (const ch of '`x`') typeNative(m, ch);
    expect(m.A.view()).toBe('"use x":- "x"{c}');
    expect(sel(m)).toEqual({ anchor: 5, focus: 5 });
    // Placed outside the code span: the next character is plain.
    typeNative(m, 'y');
    expect(m.A.view()).toBe('"use xy":- "x"{c}');
  });

  it('a fence and a table row on Enter', () => {
    const m = mount(docOf('```py', 'after'));
    m.view.setSelection(5);
    beforeInput(m.root, 'insertParagraph');
    expect(m.A.view()).toBe('"":code lang=py\n"after":-');
    typeNative(m, 'x');
    beforeInput(m.root, 'insertParagraph');
    for (const ch of '```') typeNative(m, ch);
    beforeInput(m.root, 'insertParagraph');
    expect(m.A.view()).toBe('"x":code lang=py\n"":-\n"after":-');
    const t = mount(docOf('|a|b|'));
    t.view.setSelection(5);
    beforeInput(t.root, 'insertParagraph');
    expect(t.A.view()).toBe(`"a${SEP}b":table_head cells=["a","b"]\n"${SEP}":table cells=["",""]`);
    expect(sel(t)).toEqual({ anchor: 4, focus: 4 });
    t.undo.undo();
    expect(t.A.view()).toBe('"|a|b|":-');
  });
});

describe('clipboard', () => {
  it('copy puts the selection markdown and html on the clipboard', () => {
    const m = mount(docOf('Title:h1', 'some bold text', 'item:bullet'));
    toggleInline(m.A.doc, 11, 15, 'bold');
    m.view.update();
    m.view.setSelection(2, 20);
    const { event, data } = clipboard(m.root, 'copy');
    expect(event.defaultPrevented).toBe(true);
    expect(data['text/plain']).toBe('# tle\nsome **bold** text');
    expect(data['text/html']).toContain('<strong>bold</strong>');
    expect(data['text/html']).not.toContain(FILLER);
    m.view.setSelection(3);
    expect(clipboard(m.root, 'copy').event.defaultPrevented).toBe(false);
  });

  it('cut copies then deletes as one undoable command', () => {
    const m = mount(docOf('one', 'two'));
    m.view.setSelection(1, 5);
    const { data } = clipboard(m.root, 'cut');
    expect(data['text/plain']).toBe('ne\nt');
    expect(m.A.text()).toBe('owo\n');
    expect(sel(m)).toEqual({ anchor: 1, focus: 1 });
    m.undo.undo();
    expect(m.A.text()).toBe('one\ntwo\n');
  });

  it('pastes a plain paragraph inline, with its marks', () => {
    const m = mount(docOf('ab'));
    m.view.setSelection(1);
    const { event } = clipboard(m.root, 'paste', { 'text/plain': 'x **y**' });
    expect(event.defaultPrevented).toBe(true);
    expect(m.A.view()).toBe('"ax yb":- "y"{b}');
    expect(sel(m)).toEqual({ anchor: 4, focus: 4 });
    m.undo.undo();
    expect(m.A.text()).toBe('ab\n');
  });

  it('pastes line structure as whole lines, splitting the host line', () => {
    const paste = (m: Mounted) => clipboard(m.root, 'paste', { 'text/plain': '- one\n- two\n' });
    const mid = mount(docOf('ab:h2'));
    mid.view.setSelection(1);
    paste(mid);
    expect(mid.A.view()).toBe('"a":h2\n"one":bullet\n"two":bullet\n"b":-');
    expect(sel(mid)).toEqual({ anchor: 9, focus: 9 });
    const start = mount(docOf('ab:h2'));
    start.view.setSelection(0);
    paste(start);
    expect(start.A.view()).toBe('"one":bullet\n"two":bullet\n"ab":h2');
    const end = mount(docOf('ab:h2', 'c'));
    end.view.setSelection(2);
    paste(end);
    expect(end.A.view()).toBe('"ab":h2\n"one":bullet\n"two":bullet\n"c":-');
    const empty = mount(docOf('x', '', 'y:quote'));
    empty.view.setSelection(2);
    paste(empty);
    expect(empty.A.view()).toBe('"x":-\n"one":bullet\n"two":bullet\n"y":quote');
    empty.undo.undo();
    expect(empty.A.view()).toBe('"x":-\n"":-\n"y":quote');
    const last = mount(docOf('x', ''));
    last.view.setSelection(2);
    paste(last);
    expect(last.A.view()).toBe('"x":-\n"one":bullet\n"two":bullet');
    expect(last.A.text().endsWith('\n')).toBe(true);
  });

  it('pasted text after bold text is not bold', () => {
    const m = mount(docOf('ab'));
    toggleInline(m.A.doc, 0, 2, 'bold');
    m.view.update();
    m.view.setSelection(2);
    clipboard(m.root, 'paste', { 'text/plain': 'cd' });
    expect(m.A.view()).toBe('"abcd":- "ab"{b}');
    m.view.setSelection(1);
    clipboard(m.root, 'paste', { 'text/plain': '- x\n- y\n' });
    expect(m.A.view()).toBe('"a":- "a"{b}\n"x":bullet\n"y":bullet\n"bcd":- "b"{b}');
  });

  it('pastes into a table cell as one line without separators', () => {
    const m = mount(docOf('x'));
    insertTable(m.A.doc, 0, 2, 1);
    m.view.update();
    m.view.setSelection(0);
    clipboard(m.root, 'paste', { 'text/plain': 'a\nb | c\n' });
    expect(m.A.view()).toBe(`"a b | c${SEP}":table_head cells=["a b | c",""]\n"x":-`);
    expect(cellPasteText(`a${CELL_SEP}b\n\nc`)).toBe('a b c');
  });

  it('replaces a selection, accepts a large paste and refuses a huge one', () => {
    const m = mount(docOf('abc'), { pasteLimit: 100 });
    m.view.setSelection(0, 3);
    clipboard(m.root, 'paste', { 'text/plain': 'x'.repeat(100) });
    expect(m.A.text()).toBe('x'.repeat(100) + '\n');
    clipboard(m.root, 'paste', { 'text/plain': 'y'.repeat(101) });
    expect(m.tooLarge).toEqual([101]);
    expect(m.A.text()).toBe('x'.repeat(100) + '\n');
    expect(clipboard(m.root, 'paste', { 'text/plain': '' }).event.defaultPrevented).toBe(true);
    // The default limit lets a 20k paste through.
    const big = mount(docOf(''));
    big.view.setSelection(0);
    clipboard(big.root, 'paste', { 'text/plain': ('word '.repeat(10) + '\n').repeat(400) });
    expect(big.tooLarge).toEqual([]);
    expect(lines(big.A.doc).length).toBe(400);
    expect(big.A.web.takeDeltas().length).toBeGreaterThan(0);
  });

  it('beforeinput insertFromPaste and insertFromDrop are commands too', () => {
    const m = mount(docOf('ab'));
    m.view.setSelection(2);
    const dt = { getData: (k: string) => (k === 'text/plain' ? 'cd' : '') };
    const e = new InputEvent('beforeinput', { inputType: 'insertFromDrop', bubbles: true, cancelable: true });
    Object.defineProperty(e, 'dataTransfer', { value: dt });
    m.root.dispatchEvent(e);
    expect(e.defaultPrevented).toBe(true);
    expect(m.A.text()).toBe('abcd\n');
  });

  it('selection models and markdown', () => {
    const A = docOf('Head:h1', 'a b', 'c:bullet');
    toggleInline(A.doc, 7, 8, 'bold');
    const ls = lines(A.doc);
    expect(selectionModel(ls, 5, 9)).toEqual<NotesModel>({
      text: 'a b\n',
      lines: [{ kind: null }],
      inline: [{ from: 2, to: 3, kind: 'bold', value: 'true' }]
    });
    expect(selectionMarkdown(ls, 0, 11)).toBe('# Head\na **b**\n- c\n');
    expect(selectionMarkdown(ls, 0, 10)).toBe('# Head\na **b**\n- c');
    expect(selectionMarkdown(ls, 2, 4)).toBe('ad');
    const T = docOf('x');
    insertTable(T.doc, 0, 2, 1);
    insertText(T.doc, 0, 'p');
    insertText(T.doc, 2, 'q');
    expect(selectionMarkdown(lines(T.doc), 0, 3)).toBe('p | q');
    expect(parsePaste('plain')).toEqual({ kind: 'inline', model: { text: 'plain', lines: [], inline: [] } });
    expect(parsePaste('# h').kind).toBe('block');
    expect(parsePaste('a\n\nb').kind).toBe('block');
  });
});

describe('IME', () => {
  it('defers rendering during a composition and commits the composed text once', () => {
    const { relay, A, B } = pair();
    ensureTerminated(A.doc);
    insertText(A.doc, 0, 'ab\ncd');
    relay.settle();
    const m = mount(A);
    m.view.setSelection(4); // c|d
    m.root.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }));
    expect(m.view.deferUpdates).toBe(true);
    expect(m.input.composing).toBe(true);
    // The IME writes into the DOM; nothing is committed.
    const t = m.root.children[1].firstChild as Text;
    t.data = 'cかd';
    (document.getSelection() as Selection).setBaseAndExtent(t, 2, t, 2);
    m.root.dispatchEvent(new InputEvent('beforeinput', { inputType: 'insertCompositionText', data: 'か', isComposing: true, bubbles: true, cancelable: true }));
    m.root.dispatchEvent(new InputEvent('input', { inputType: 'insertCompositionText', data: 'か', isComposing: true, bubbles: true }));
    expect(A.text()).toBe('ab\ncd\n');
    // A remote edit lands in the model now and in the DOM later.
    insertText(B.doc, 0, 'R');
    m.input.applyRemote(() => relay.settle());
    expect(A.text()).toBe('Rab\ncd\n');
    expect(extractText(m.root)).toBe('ab\ncかd\n');
    t.data = 'c漢d';
    (document.getSelection() as Selection).setBaseAndExtent(t, 2, t, 2);
    m.root.dispatchEvent(new CompositionEvent('compositionend', { data: '漢', bubbles: true }));
    expect(m.view.deferUpdates).toBe(false);
    expect(A.text()).toBe('Rab\nc漢d\n');
    expect(extractText(m.root)).toBe('Rab\nc漢d\n');
    expect(sel(m)).toEqual({ anchor: 6, focus: 6 });
    m.undo.undo();
    expect(A.text()).toBe('Rab\ncd\n');
  });

  it('keeps remote text that landed in the composed line', () => {
    const { relay, A, B } = pair();
    ensureTerminated(A.doc);
    insertText(A.doc, 0, 'ab');
    relay.settle();
    const m = mount(A);
    m.view.setSelection(1);
    m.root.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }));
    const t = m.root.children[0].firstChild as Text;
    t.data = 'aかb';
    (document.getSelection() as Selection).setBaseAndExtent(t, 2, t, 2);
    insertText(B.doc, 0, 'R');
    insertText(B.doc, 3, 'Z');
    m.input.applyRemote(() => relay.settle());
    expect(A.text()).toBe('RabZ\n');
    t.data = 'a漢b';
    (document.getSelection() as Selection).setBaseAndExtent(t, 2, t, 2);
    m.root.dispatchEvent(new CompositionEvent('compositionend', { data: '漢', bubbles: true }));
    expect(A.text()).toBe('Ra漢bZ\n');
    expect(extractText(m.root)).toBe('Ra漢bZ\n');
    expect(sel(m)).toEqual({ anchor: 3, focus: 3 });
    // A composition that replaced a selection the other side had grown.
    m.view.setSelection(1, 4); // "a漢b"
    m.root.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }));
    const t2 = m.root.children[0].firstChild as Text;
    t2.data = 'RxZ';
    (document.getSelection() as Selection).setBaseAndExtent(t2, 2, t2, 2);
    insertText(B.doc, 2, 'Q'); // inside the selected text
    m.input.applyRemote(() => relay.settle());
    expect(A.text()).toBe('RaQ漢bZ\n');
    m.root.dispatchEvent(new CompositionEvent('compositionend', { data: 'x', bubbles: true }));
    // The removal is by element id: the other side's concurrent insert survives, as it would
    // for any concurrent delete, and the composed text lands after its anchor.
    expect(A.text()).toBe('RxQZ\n');
    expect(extractText(m.root)).toBe('RxQZ\n');
  });
});

describe('remote changes and undo', () => {
  it('applyRemote keeps the caret on its characters and adds no undo entry', () => {
    const { relay, A, B } = pair();
    ensureTerminated(A.doc);
    insertText(A.doc, 0, 'hello');
    relay.settle();
    const m = mount(A);
    m.view.setSelection(3);
    insertText(B.doc, 0, 'REMOTE ');
    m.input.applyRemote(() => relay.settle());
    expect(extractText(m.root)).toBe('REMOTE hello\n');
    expect(sel(m)).toEqual({ anchor: 10, focus: 10 });
    expect(m.undo.canUndo()).toBe(false);
    expect(m.changes).toBe(0);
  });

  it('undo after a remote edit removes only our text; redo brings it back', () => {
    const { relay, A, B } = pair();
    ensureTerminated(A.doc);
    insertText(A.doc, 0, 'ab');
    relay.settle();
    const m = mount(A);
    m.view.setSelection(2);
    typeNative(m, 'c');
    relay.settle();
    insertText(B.doc, 0, 'X');
    m.input.applyRemote(() => relay.settle());
    expect(A.text()).toBe('Xabc\n');
    key(m.root, 'z', { ctrlKey: true });
    expect(A.text()).toBe('Xab\n');
    expect(extractText(m.root)).toBe('Xab\n');
    key(m.root, 'y', { ctrlKey: true });
    expect(A.text()).toBe('Xabc\n');
    relay.settle();
    expect(B.text()).toBe('Xabc\n');
  });

  it('a local change notifies and the frames converge', () => {
    const { relay, A, B } = pair();
    ensureTerminated(A.doc);
    const m = mount(A);
    m.view.setSelection(0);
    beforeInput(m.root, 'insertText', 'one\ntwo');
    expect(m.changes).toBe(1);
    for (const f of drain(A.web)) applyRemote(B.web, f);
    expect(viewOf(lines(B.doc))).toBe(viewOf(lines(A.doc)));
    relay.settle();
  });
});

describe('read-only', () => {
  it('refuses edits, shortcuts and pastes but still copies', () => {
    const m = mount(docOf('hello'), { readOnly: true });
    expect(m.root.contentEditable).toBe('false');
    m.view.setSelection(0, 5);
    expect(beforeInput(m.root, 'insertText', 'x').defaultPrevented).toBe(true);
    key(m.root, 'b', { ctrlKey: true });
    beforeInput(m.root, 'insertParagraph');
    clipboard(m.root, 'paste', { 'text/plain': 'nope' });
    expect(clipboard(m.root, 'copy').data['text/plain']).toBe('hello');
    expect(clipboard(m.root, 'cut').data['text/plain']).toBe('hello');
    m.input.setLink(0, 5, 'https://x/');
    m.input.toggleInline('bold');
    m.input.setLineKind('h1');
    expect(m.A.view()).toBe('"hello":-');
    expect(m.changes).toBe(0);
    m.input.setReadOnly(false);
    expect(m.root.contentEditable).toBe('true');
    m.view.setSelection(0, 5);
    key(m.root, 'b', { ctrlKey: true });
    expect(m.A.view()).toBe('"hello":- "hello"{b}');
    m.input.detach();
    key(m.root, 'b', { ctrlKey: true });
    expect(m.A.view()).toBe('"hello":- "hello"{b}');
  });
});

describe('component API', () => {
  it('toggleInline, setLineKind, undo and redo act on the selection', () => {
    const m = mount(docOf('ab'));
    m.view.setSelection(0, 2);
    m.input.toggleInline('italic');
    m.input.setLineKind('h3');
    expect(m.A.view()).toBe('"ab":h3 "ab"{i}');
    m.input.undo();
    m.input.undo();
    expect(m.A.view()).toBe('"ab":-');
    m.input.redo();
    expect(m.A.view()).toBe('"ab":- "ab"{i}');
    expect(JSON.parse(parseMarkdown('x')).lines).toHaveLength(1);
  });
});

describe('hooks for menus (phase 11)', () => {
  it('onTyped hears a natively typed character with the caret after it', () => {
    const typed: [string, number][] = [];
    const m = mount(docOf('ab'), { hooks: { onTyped: (c, caret) => typed.push([c, caret]) } });
    m.view.setSelection(1);
    typeNative(m, '/');
    expect(m.A.text()).toBe('a/b\n');
    expect(typed).toEqual([['/', 2]]);
    typeNative(m, 'x');
    expect(typed).toEqual([['/', 2], ['x', 3]]);
  });

  it('onTyped also hears a character the browser put outside every line (WebKit, a first key in an empty editor)', () => {
    // NotesEditor terminates an empty doc on the first beforeinput and renders its line under a
    // caret that was on the root: WebKit then inserts the text before the line element.
    const typed: [string, number][] = [];
    const m = mount(docOf(''), { hooks: { onTyped: (c, caret) => typed.push([c, caret]) } });
    (document.getSelection() as Selection).setBaseAndExtent(m.root, 0, m.root, 0);
    typeNative(m, '/');
    expect(m.A.text()).toBe('/\n');
    expect(m.root.querySelectorAll('.ne-line')).toHaveLength(1);
    expect(typed).toEqual([['/', 1]]);
    expect(m.view.getSelection()).toEqual({ anchor: 1, focus: 1 });
  });

  it('onTyped is not called for a deletion, a paste, a command or a multi-character insert', () => {
    const typed: string[] = [];
    const m = mount(docOf('hello'), { hooks: { onTyped: (c) => typed.push(c) } });
    m.view.setSelection(3);
    backspaceNative(m);
    clipboard(m.root, 'paste', { 'text/plain': '/' });
    m.input.command((doc) => insertText(doc, 0, '/'));
    expect(typed).toEqual([]);
    // An autocorrect-like insert of two characters is not "one typed character".
    typeNative(m, 'ab');
    expect(typed).toEqual([]);
  });

  it('onTyped is not called during a composition nor for the commit that ends it', () => {
    const typed: string[] = [];
    const m = mount(docOf('ab'), { hooks: { onTyped: (c) => typed.push(c) } });
    m.view.setSelection(1);
    m.root.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }));
    const t = m.root.children[0].firstChild as Text;
    t.data = 'a/b';
    (document.getSelection() as Selection).setBaseAndExtent(t, 2, t, 2);
    m.root.dispatchEvent(new InputEvent('beforeinput', { inputType: 'insertCompositionText', data: '/', isComposing: true, bubbles: true, cancelable: true }));
    m.root.dispatchEvent(new InputEvent('input', { inputType: 'insertCompositionText', data: '/', isComposing: true, bubbles: true }));
    expect(typed).toEqual([]);
    m.root.dispatchEvent(new CompositionEvent('compositionend', { data: '/', bubbles: true }));
    expect(m.A.text()).toBe('a/b\n');
    expect(typed).toEqual([]);
    // Typing right after the composition is a plain keystroke again.
    typeNative(m, 'x');
    expect(typed).toEqual(['x']);
  });

  it('interceptKey sees keydown first; true cancels it and Enter does not split', () => {
    const seen: string[] = [];
    let take = true;
    const m = mount(docOf('hello'), {
      hooks: {
        interceptKey: (e) => {
          seen.push(e.key);
          return take && (e.key === 'Enter' || e.key === 'ArrowDown');
        }
      }
    });
    m.view.setSelection(2);
    // What an engine does: beforeinput(insertParagraph) only follows a keydown nobody cancelled.
    const pressEnter = () => {
      const e = key(m.root, 'Enter');
      if (!e.defaultPrevented) beforeInput(m.root, 'insertParagraph');
      return e;
    };
    expect(pressEnter().defaultPrevented).toBe(true);
    expect(m.A.text()).toBe('hello\n');
    expect(m.changes).toBe(0);
    expect(key(m.root, 'ArrowDown').defaultPrevented).toBe(true);
    // Not intercepted: the editor's own arrow handler runs (and cancels the event itself).
    expect(key(m.root, 'ArrowRight').defaultPrevented).toBe(true);
    expect(sel(m)).toEqual({ anchor: 3, focus: 3 });
    expect(seen).toEqual(['Enter', 'ArrowDown', 'ArrowRight']);
    take = false;
    expect(pressEnter().defaultPrevented).toBe(false);
    expect(m.A.text()).toBe('hel\nlo\n'); // split at the caret ArrowRight moved to
  });

  it('interceptKey is not asked while read-only or composing', () => {
    const seen: string[] = [];
    const hooks = { interceptKey: (e: KeyboardEvent) => (seen.push(e.key), true) };
    const m = mount(docOf('ab'), { readOnly: true, hooks });
    key(m.root, 'Enter');
    expect(seen).toEqual([]);
    m.input.setReadOnly(false);
    m.view.setSelection(1);
    m.root.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }));
    key(m.root, 'Enter');
    expect(seen).toEqual([]);
  });

  it('command() runs a CommandResult as one undo entry, with the caret placed', () => {
    const m = mount(docOf('abc'));
    m.view.setSelection(1);
    const r = m.input.command((doc) => {
      const a = insertText(doc, 3, 'xy');
      const b = setLink(doc, 3, 5, 'https://example.com');
      return { inverse: [...a.inverse, ...b.inverse], caret: 5 };
    });
    expect(r?.caret).toBe(5);
    expect(m.A.text()).toBe('abcxy\n');
    expect(m.changes).toBe(1);
    expect(sel(m)).toEqual({ anchor: 5, focus: 5 });
    expect(extractText(m.root)).toBe('abcxy\n');
    m.input.undo();
    expect(m.A.text()).toBe('abc\n');
    expect(m.undo.canUndo()).toBe(false);
    m.input.redo();
    expect(m.A.text()).toBe('abcxy\n');
    // An explicit placement wins over the command's caret.
    m.input.command((doc) => insertText(doc, 0, 'Z'), { caret: 0, focus: 2 });
    expect(sel(m)).toEqual({ anchor: 0, focus: 2 });
  });

  it('command() does nothing on a read-only editor', () => {
    const m = mount(docOf('abc'), { readOnly: true });
    expect(m.input.command((doc) => insertText(doc, 0, 'x'))).toBeNull();
    expect(m.A.text()).toBe('abc\n');
  });

  it('setLineKind toggles off by default and sets with toggle: false', () => {
    const m = mount(docOf('ab'));
    m.view.setSelection(0, 2);
    m.input.setLineKind('h1');
    expect(m.A.view()).toBe('"ab":h1');
    m.input.setLineKind('h1');
    expect(m.A.view()).toBe('"ab":-');
    m.input.setLineKind('h1');
    m.input.setLineKind('h1', { toggle: false });
    expect(m.A.view()).toBe('"ab":h1');
    m.input.setLineKind('task_done' as never, { toggle: false });
    m.input.setLineKind('task', { toggle: false });
    expect(m.A.view()).toBe('"ab":task');
    m.input.setLineKind(null, { toggle: false });
    expect(m.A.view()).toBe('"ab":-');
  });
});

describe('chips (phase 11)', () => {
  const CHIP = 'mnemosyne:t/0/65';
  /** "see 1:05 now": the chip covers [4, 8). */
  const chipped = (opts: Parameters<typeof mount>[1] = {}) => {
    const A = docOf('see 1:05 now');
    setLink(A.doc, 4, 8, CHIP);
    return mount(A, opts);
  };

  it('Backspace right after a chip removes the whole chip as one undo entry', () => {
    const m = chipped();
    m.view.setSelection(8);
    const e = key(m.root, 'Backspace');
    expect(e.defaultPrevented).toBe(true);
    expect(m.A.text()).toBe('see  now\n');
    expect(sel(m)).toEqual({ anchor: 4, focus: 4 });
    m.undo.undo();
    expect(m.A.text()).toBe('see 1:05 now\n');
    expect(m.A.view()).toBe('"see 1:05 now":- "1:05"{link=' + CHIP + '}');
  });

  it('Delete right before a chip removes the whole chip as one undo entry', () => {
    const m = chipped();
    m.view.setSelection(4);
    expect(key(m.root, 'Delete').defaultPrevented).toBe(true);
    expect(m.A.text()).toBe('see  now\n');
    expect(sel(m)).toEqual({ anchor: 4, focus: 4 });
    m.undo.undo();
    expect(m.A.view()).toBe('"see 1:05 now":- "1:05"{link=' + CHIP + '}');
  });

  it('inside a chip, or away from its edge, Backspace and Delete are the browser\'s', () => {
    const m = chipped();
    m.view.setSelection(6);
    expect(key(m.root, 'Backspace').defaultPrevented).toBe(false);
    expect(key(m.root, 'Delete').defaultPrevented).toBe(false);
    m.view.setSelection(8); // Delete after the chip, Backspace before it
    expect(key(m.root, 'Delete').defaultPrevented).toBe(false);
    m.view.setSelection(4);
    expect(key(m.root, 'Backspace').defaultPrevented).toBe(false);
    expect(m.A.text()).toBe('see 1:05 now\n');
    m.view.setSelection(8, 10); // a selection is not a collapsed delete
    expect(key(m.root, 'Backspace').defaultPrevented).toBe(false);
  });

  it('removes a chip at the end of its line and leaves the other chip alone', () => {
    const A = docOf('1:05 Ada');
    setLink(A.doc, 0, 4, CHIP);
    setLink(A.doc, 5, 8, 'mnemosyne:person/Ada');
    const m = mount(A);
    m.view.setSelection(8);
    key(m.root, 'Backspace');
    expect(m.A.text()).toBe('1:05 \n');
    const linked = (c: Client) => lines(c.doc).flatMap((l) => l.runs.filter((r) => r.link).map((r) => r.text));
    expect(linked(m.A)).toEqual(['1:05']);
  });

  it('a read-only editor deletes nothing', () => {
    const m = chipped({ readOnly: true });
    m.view.setSelection(8);
    key(m.root, 'Backspace');
    expect(m.A.text()).toBe('see 1:05 now\n');
  });

  it('Ctrl+K on a chip asks for nothing; an ordinary link still does', () => {
    const m = chipped();
    m.view.setSelection(6);
    key(m.root, 'k', { ctrlKey: true });
    m.view.setSelection(8);
    key(m.root, 'k', { ctrlKey: true });
    m.view.setSelection(4, 8);
    key(m.root, 'k', { ctrlKey: true });
    m.view.setSelection(0, 6); // a selection reaching into the chip
    key(m.root, 'k', { ctrlKey: true });
    expect(m.links).toEqual([]);
    m.view.setSelection(0, 3);
    key(m.root, 'k', { ctrlKey: true });
    expect(m.links).toEqual([{ from: 0, to: 3, url: null }]);
  });

  it('requestLink() asks for a plain selection and not for a chip', () => {
    const m = chipped();
    m.view.setSelection(4, 8);
    m.input.requestLink();
    m.view.setSelection(0, 6);
    m.input.requestLink();
    expect(m.links).toEqual([]);
    m.view.setSelection(0, 3);
    m.input.requestLink();
    expect(m.links).toEqual([{ from: 0, to: 3, url: null }]);
    m.input.setReadOnly(true);
    m.input.requestLink();
    expect(m.links).toHaveLength(1);
  });

  it('typing right after a chip is plain text, not part of the chip', () => {
    const m = chipped();
    m.view.setSelection(8);
    typeNative(m, 'x');
    expect(m.A.text()).toBe('see 1:05x now\n');
    expect(m.A.view()).toBe('"see 1:05x now":- "1:05"{link=' + CHIP + '}');
  });

  it('typing inside a chip keeps it one chip, in one undo step with the typing', () => {
    const m = chipped();
    m.view.setSelection(6);
    typeNative(m, 'x');
    expect(m.A.text()).toBe('see 1:x05 now\n');
    const runs = () => lines(m.A.doc)[0].runs.map((r) => [r.text, r.link]);
    expect(runs()).toEqual([['see ', null], ['1:x05', CHIP], [' now', null]]);
    expect(m.root.querySelectorAll('a.ne-chip')).toHaveLength(1);
    expect(m.root.querySelector('a.ne-chip')?.textContent).toBe('1:x05');
    m.undo.undo();
    expect(m.A.text()).toBe('see 1:05 now\n');
    expect(runs()).toEqual([['see ', null], ['1:05', CHIP], [' now', null]]);
  });

  it('a paste inside a chip keeps it one chip; one at its edge does not extend it', () => {
    const m = chipped();
    m.view.setSelection(6);
    clipboard(m.root, 'paste', { 'text/plain': 'zz' });
    expect(m.A.text()).toBe('see 1:zz05 now\n');
    expect(lines(m.A.doc)[0].runs.map((r) => [r.text, r.link])).toEqual([['see ', null], ['1:zz05', CHIP], [' now', null]]);
    m.view.setSelection(10); // right after the chip
    clipboard(m.root, 'paste', { 'text/plain': 'q' });
    expect(lines(m.A.doc)[0].runs.map((r) => [r.text, r.link])).toEqual([['see ', null], ['1:zz05', CHIP], ['q now', null]]);
  });

  it('replacing the middle of a chip keeps it one chip', () => {
    const m = chipped();
    m.view.setSelection(5, 7); // ":0"
    clipboard(m.root, 'paste', { 'text/plain': 'x' });
    expect(m.A.text()).toBe('see 1x5 now\n');
    expect(lines(m.A.doc)[0].runs.map((r) => [r.text, r.link])).toEqual([['see ', null], ['1x5', CHIP], [' now', null]]);
  });

  it('two adjacent chips with different URLs delete one at a time', () => {
    const A = docOf('AdaBob');
    setLink(A.doc, 0, 3, 'mnemosyne:person/Ada');
    setLink(A.doc, 3, 6, 'mnemosyne:person/Bob');
    const m = mount(A);
    m.view.setSelection(3);
    key(m.root, 'Backspace');
    expect(m.A.text()).toBe('Bob\n');
    m.undo.undo();
    m.view.setSelection(3);
    key(m.root, 'Delete');
    expect(m.A.text()).toBe('Ada\n');
    // typing between them is plain text and joins neither
    key(m.root, 'z', { ctrlKey: true }); // through the editor, so the DOM is redrawn
    expect(m.A.text()).toBe('AdaBob\n');
    m.view.setSelection(3);
    typeNative(m, 'x');
    expect(m.A.text()).toBe('AdaxBob\n');
    expect(lines(m.A.doc)[0].runs.map((r) => [r.text, r.link])).toEqual([
      ['Ada', 'mnemosyne:person/Ada'],
      ['x', null],
      ['Bob', 'mnemosyne:person/Bob']
    ]);
  });
});
