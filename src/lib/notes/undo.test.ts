import { describe, expect, it } from 'vitest';
import { converged, pair, single, type Client } from './test-harness.js';
import {
  addColumn,
  deleteRange,
  insertTable,
  insertText,
  setLineKind,
  setLink,
  splitLine,
  toggleInline,
  toggleTask
} from './commands.js';
import { ensureTerminated, lines } from './model.js';
import { UndoStack } from './undo.js';

function fresh(text = ''): { A: Client; undo: UndoStack; clock: { t: number } } {
  const A = single();
  ensureTerminated(A.doc);
  if (text) insertText(A.doc, 0, text);
  const clock = { t: 0 };
  const undo = new UndoStack(A.doc, { now: () => clock.t, groupMs: 1000 });
  return { A, undo, clock };
}

describe('undo and redo', () => {
  it('walk a mixed history back and forth', () => {
    const { A, undo } = fresh();
    const views: string[] = [A.view()];
    const step = (r: { inverse: never[] | ReturnType<typeof insertText>['inverse'] }) => {
      undo.push(r.inverse);
      views.push(A.view());
    };
    step(insertText(A.doc, 0, 'Hello world'));
    step(setLineKind(A.doc, 0, 0, 'h2'));
    step(splitLine(A.doc, 5));
    step(toggleInline(A.doc, 0, 3, 'bold'));
    step(setLink(A.doc, 7, 12, 'https://example.com/undo'));
    step(toggleTask(A.doc, 7));
    step(insertTable(A.doc, 0, 2, 2));
    step(addColumn(A.doc, 0, 1));
    step(deleteRange(A.doc, 0, A.web.textLen(A.doc.obj)));
    for (let i = views.length - 1; i > 0; i--) {
      expect(A.view(), `at ${i}`).toBe(views[i]);
      expect(undo.undo()).not.toBeUndefined();
      expect(A.view(), `after undo ${i}`).toBe(views[i - 1]);
    }
    expect(undo.canUndo()).toBe(false);
    for (let i = 1; i < views.length; i++) {
      undo.redo();
      expect(A.view(), `after redo ${i}`).toBe(views[i]);
    }
    expect(undo.canRedo()).toBe(false);
    expect(A.text().endsWith('\n')).toBe(true);
  });

  it('groups consecutive typing, broken by a word boundary or a pause', () => {
    const { A, undo, clock } = fresh();
    for (const ch of 'hello') {
      undo.push(insertText(A.doc, A.web.textLen(A.doc.obj) - 1, ch).inverse, { typed: ch });
      clock.t += 100;
    }
    undo.push(insertText(A.doc, 5, ' ').inverse, { typed: ' ' });
    for (const ch of 'world') undo.push(insertText(A.doc, A.web.textLen(A.doc.obj) - 1, ch).inverse, { typed: ch });
    clock.t += 5000;
    undo.push(insertText(A.doc, 11, '!').inverse, { typed: '!' });
    expect(A.text()).toBe('hello world!\n');
    undo.undo();
    expect(A.text()).toBe('hello world\n');
    undo.undo();
    expect(A.text()).toBe('hello\n');
    undo.undo();
    expect(A.text()).toBe('\n');
    expect(undo.canUndo()).toBe(false);
  });

  it('a new edit clears the redo stack', () => {
    const { A, undo } = fresh();
    undo.push(insertText(A.doc, 0, 'a').inverse);
    undo.undo();
    undo.push(insertText(A.doc, 0, 'b').inverse);
    expect(undo.canRedo()).toBe(false);
    expect(A.text()).toBe('b\n');
  });
});

describe('undo after a remote edit', () => {
  it('re-inserts deleted text next to its neighbours, wherever they moved', () => {
    const { relay, A, B } = pair();
    ensureTerminated(A.doc);
    insertText(A.doc, 0, 'one two three');
    relay.settle();
    const undo = new UndoStack(A.doc);
    undo.push(deleteRange(A.doc, 4, 7).inverse); // "two"
    relay.settle();
    insertText(B.doc, 0, 'ZERO ');
    toggleInline(B.doc, 0, 4, 'bold');
    relay.settle();
    expect(A.text()).toBe('ZERO one  three\n');
    undo.undo();
    relay.settle();
    expect(converged(A, B)).toBe(true);
    expect(A.text()).toBe('ZERO one two three\n');
    undo.redo();
    relay.settle();
    expect(converged(A, B)).toBe(true);
    expect(A.text()).toBe('ZERO one  three\n');
  });

  it('removes only what we inserted, even after remote text landed inside it', () => {
    const { relay, A, B } = pair();
    ensureTerminated(A.doc);
    insertText(A.doc, 0, 'ab');
    relay.settle();
    const undo = new UndoStack(A.doc);
    undo.push(insertText(A.doc, 1, 'XYZ').inverse);
    relay.settle();
    insertText(B.doc, 3, '-'); // between Y and Z
    relay.settle();
    expect(A.text()).toBe('aXY-Zb\n');
    undo.undo();
    relay.settle();
    expect(converged(A, B)).toBe(true);
    expect(A.text()).toBe('a-b\n');
  });

  it('restores marks on the elements they covered after a remote insert shifted them', () => {
    const { relay, A, B } = pair();
    ensureTerminated(A.doc);
    insertText(A.doc, 0, 'make this bold');
    relay.settle();
    const undo = new UndoStack(A.doc);
    undo.push(toggleInline(A.doc, 10, 14, 'bold').inverse);
    undo.push(setLineKind(A.doc, 0, 0, 'h3').inverse);
    relay.settle();
    insertText(B.doc, 0, 'please ');
    relay.settle();
    expect(A.view()).toBe('"please make this bold":h3 "bold"{b}');
    undo.undo();
    undo.undo();
    relay.settle();
    expect(converged(A, B)).toBe(true);
    expect(A.view()).toBe('"please make this bold":-');
  });

  it('undoing the deletion of a line that a remote edit later joined keeps the doc terminated', () => {
    const { relay, A, B } = pair();
    ensureTerminated(A.doc);
    insertText(A.doc, 0, 'a');
    splitLine(A.doc, 1);
    insertText(A.doc, 2, 'b');
    relay.settle();
    const undo = new UndoStack(A.doc);
    undo.push(deleteRange(A.doc, 2, 4).inverse); // "b\n" gone, then re-terminated
    relay.settle();
    deleteRange(B.doc, 0, 2); // B removes "a\n"
    relay.settle();
    expect(converged(A, B)).toBe(true);
    undo.undo();
    relay.settle();
    expect(converged(A, B)).toBe(true);
    expect(A.text().endsWith('\n')).toBe(true);
    expect(lines(A.doc).map((l) => l.text)).toContain('b');
  });
});
