import { describe, expect, it } from 'vitest';
import { pair, single } from '../test-harness.js';
import { deleteRange, insertText, setLineKind, splitLine, toggleTask } from '../commands.js';
import { ensureTerminated, lines } from '../model.js';
import { extractText, getSelectionOffsets, setSelectionOffsets } from './dom.js';
import { captureEditState, createEditorView, restoreEditState } from './view.js';

function mount(): HTMLElement {
  const root = document.createElement('div');
  root.tabIndex = 0; // jsdom needs it to focus a contenteditable; real engines do not
  document.body.appendChild(root);
  return root;
}

function docPair() {
  const p = pair();
  ensureTerminated(p.A.doc);
  insertText(p.A.doc, 0, 'first line\nsecond line\nthird');
  p.relay.settle();
  return p;
}

describe('captureEditState / restoreEditState', () => {
  it('keeps the caret on the same character after a remote insert before it', () => {
    const { relay, A, B } = docPair();
    const root = mount();
    const view = createEditorView({ root, doc: A.doc });
    view.update();
    root.focus();
    view.setSelection(13, 17); // "cond"
    const state = captureEditState(A.doc, root);
    expect(state?.anchor).toBe(A.web.seqIdAt(A.doc.obj, 12));
    view.beforeChange();
    insertText(B.doc, 0, 'REMOTE ');
    insertText(B.doc, 7 + 11, '>> ');
    relay.settle();
    view.update();
    expect(extractText(root)).toBe('REMOTE first line\n>> second line\nthird\n');
    expect(getSelectionOffsets(root)).toEqual({ anchor: 23, focus: 27 });
    expect(A.text().slice(23, 27)).toBe('cond');
  });

  it('keeps the caret after a remote delete of the line before', () => {
    const { relay, A, B } = docPair();
    const root = mount();
    const view = createEditorView({ root, doc: A.doc });
    view.update();
    root.focus();
    view.setSelection(25); // "th|ird"
    view.beforeChange();
    deleteRange(B.doc, 0, 11); // "first line\n"
    relay.settle();
    view.update();
    expect(getSelectionOffsets(root)).toEqual({ anchor: 14, focus: 14 });
    expect(A.text().slice(12, 14)).toBe('th');
  });

  it('falls back to the clamped old offset when the character was deleted', () => {
    const { relay, A, B } = docPair();
    const root = mount();
    const view = createEditorView({ root, doc: A.doc });
    view.update();
    root.focus();
    view.setSelection(28); // end of "third"
    const state = captureEditState(A.doc, root);
    view.beforeChange();
    deleteRange(B.doc, 11, 28); // "second line\nthird"
    relay.settle();
    view.update();
    expect(A.text()).toBe('first line\n\n');
    // Clamped to the end of the last (empty) line: the doc's length itself is no caret position.
    expect(getSelectionOffsets(root)).toEqual({ anchor: 11, focus: 11 });
    expect(restoreEditState(A.doc, root, state as NonNullable<typeof state>)).toEqual({ anchor: 11, focus: 11 });
  });

  it('beforeChange captures once; a second call before the update is ignored', () => {
    const { relay, A, B } = docPair();
    const root = mount();
    const view = createEditorView({ root, doc: A.doc });
    view.update();
    root.focus();
    view.setSelection(25);
    view.beforeChange();
    insertText(B.doc, 0, 'x');
    relay.settle();
    view.beforeChange(); // too late to be right; the first capture stands
    insertText(B.doc, 0, 'y');
    relay.settle();
    view.update();
    expect(getSelectionOffsets(root)).toEqual({ anchor: 27, focus: 27 });
  });

  it('records "start of doc" as null and restores the scroll position', () => {
    const A = single();
    ensureTerminated(A.doc);
    insertText(A.doc, 0, 'abc');
    const root = mount();
    const view = createEditorView({ root, doc: A.doc });
    view.update();
    root.focus();
    setSelectionOffsets(root, 0, 2);
    root.scrollTop = 40;
    const state = captureEditState(A.doc, root);
    expect(state).toMatchObject({ anchor: null, anchorOffset: 0, focusOffset: 2, scrollTop: 40 });
    root.scrollTop = 0;
    restoreEditState(A.doc, root, state as NonNullable<typeof state>);
    expect(getSelectionOffsets(root)).toEqual({ anchor: 0, focus: 2 });
    expect(root.scrollTop).toBe(40);
    document.getSelection()?.removeAllRanges();
    expect(captureEditState(A.doc, root)).toBeNull();
  });
});

describe('createEditorView', () => {
  it('renders, exposes lines, and restores the selection only when focused', () => {
    const { relay, A, B } = docPair();
    const root = mount();
    const view = createEditorView({ root, doc: A.doc });
    expect(root.classList.contains('notes-editor')).toBe(true);
    expect(root.contentEditable).toBe('true');
    view.update();
    expect(view.lines.length).toBe(3);
    expect(extractText(root)).toBe(A.text());
    // Not focused: a selection elsewhere is left alone.
    const other = document.createElement('div');
    other.textContent = 'elsewhere';
    document.body.appendChild(other);
    document.getSelection()?.setBaseAndExtent(other.firstChild as Node, 1, other.firstChild as Node, 1);
    insertText(B.doc, 0, 'x');
    relay.settle();
    view.update();
    expect(document.getSelection()?.anchorNode).toBe(other.firstChild);
    expect(getSelectionOffsets(root)).toBeNull();
    other.remove();
  });

  it('queues one update while deferUpdates is set', () => {
    const { relay, A, B } = docPair();
    const root = mount();
    const view = createEditorView({ root, doc: A.doc });
    view.update();
    view.deferUpdates = true;
    insertText(B.doc, 0, 'a');
    relay.settle();
    view.update();
    insertText(B.doc, 0, 'b');
    relay.settle();
    view.update();
    expect(extractText(root)).toBe('first line\nsecond line\nthird\n');
    view.deferUpdates = false;
    expect(extractText(root)).toBe('bafirst line\nsecond line\nthird\n');
  });

  it('readOnly sets contenteditable=false and ignores checkbox clicks', () => {
    const A = single();
    ensureTerminated(A.doc);
    insertText(A.doc, 0, 'todo');
    setLineKind(A.doc, 0, 0, 'task');
    const root = mount();
    const toggled: number[] = [];
    const view = createEditorView({ root, doc: A.doc, readOnly: true, onToggleTask: (i) => toggled.push(i) });
    view.update();
    expect(root.contentEditable).toBe('false');
    (root.querySelector('.ne-check') as HTMLElement).dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(toggled).toEqual([]);
    view.readOnly = false;
    expect(root.contentEditable).toBe('true');
  });

  it('a checkbox click reports the line index; toggling re-renders it', () => {
    const A = single();
    ensureTerminated(A.doc);
    insertText(A.doc, 0, 'plain\ntodo\nmore');
    setLineKind(A.doc, 6, 6, 'task');
    splitLine(A.doc, 10);
    const root = mount();
    const toggled: number[] = [];
    const view = createEditorView({
      root,
      doc: A.doc,
      onToggleTask: (i) => {
        toggled.push(i);
        toggleTask(A.doc, view.lines[i].start);
        view.update();
      }
    });
    view.update();
    const box = root.querySelector('.ne-check') as HTMLElement;
    const down = new MouseEvent('mousedown', { bubbles: true, cancelable: true });
    box.dispatchEvent(down);
    expect(down.defaultPrevented).toBe(true);
    box.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(toggled).toEqual([1]);
    expect(lines(A.doc)[1].kind).toBe('task_done');
    expect((root.querySelector('.ne-check') as HTMLElement).getAttribute('aria-checked')).toBe('true');
    expect(root.children[1].className).toBe('ne-line ne-task_done');
    view.destroy();
    box.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(toggled).toEqual([1]);
  });

  it('invalidate forces a rebuild of an unchanged line', () => {
    const A = single();
    ensureTerminated(A.doc);
    insertText(A.doc, 0, 'abc');
    const root = mount();
    const view = createEditorView({ root, doc: A.doc });
    view.update();
    const el = root.firstElementChild as HTMLElement;
    el.firstChild!.textContent = 'abX'; // a native edit the model does not have
    view.update();
    expect(root.firstElementChild).toBe(el);
    expect(extractText(root)).toBe('abX\n');
    view.invalidate(view.lines[0].id);
    view.update();
    expect(root.firstElementChild).not.toBe(el);
    expect(extractText(root)).toBe('abc\n');
  });

  it('maps points and lines through the view helpers', () => {
    const A = single();
    ensureTerminated(A.doc);
    insertText(A.doc, 0, 'ab\ncd');
    const root = mount();
    const view = createEditorView({ root, doc: A.doc });
    view.update();
    const p = view.pointAt(4);
    expect(view.offsetOf(p.node, p.offset)).toBe(4);
    expect(view.lineOf(p.node)).toEqual({ el: root.children[1], id: view.lines[1].id });
    expect(view.lineOf(document.body)).toBeNull();
  });
});
