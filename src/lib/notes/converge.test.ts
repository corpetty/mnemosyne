// Every command run concurrently on two replicas, frames exchanged through a relay that never
// echoes: byte-equal encode() and equal lines() afterwards.
import { describe, expect, it } from 'vitest';
import { converged, pair, type Client, type Relay } from './test-harness.js';
import {
  addColumn,
  addComment,
  addRow,
  applyInputRule,
  deleteRange,
  inputRule,
  insertModel,
  insertTable,
  insertText,
  joinWithPrevious,
  nextCell,
  removeColumn,
  removeComment,
  removeRow,
  setAlign,
  setLineKind,
  setLink,
  splitLine,
  toggleInline,
  toggleTask
} from './commands.js';
import { CELL_SEP, ensureTerminated, lines } from './model.js';

function seeded(text: string): { relay: ReturnType<typeof pair>['relay']; A: Client; B: Client } {
  const { relay, A, B } = pair();
  ensureTerminated(A.doc);
  insertText(A.doc, 0, text);
  relay.settle();
  expect(B.text()).toBe(text + '\n');
  return { relay, A, B };
}

/** Run `a` on A and `b` on B from the same state, deliver, and check convergence. */
function race(text: string, a: (c: Client) => void, b: (c: Client) => void): { relay: Relay; A: Client; B: Client } {
  const { relay, A, B } = seeded(text);
  a(A);
  b(B);
  relay.settle();
  expect(A.view(), 'A vs B').toBe(B.view());
  expect(converged(A, B)).toBe(true);
  return { relay, A, B };
}

describe('concurrent commands converge', () => {
  it('insertText at the same place', () => {
    // The two runs' identical first character (" ") is ONE element: ids are content hashes.
    const { A } = race('hello', (c) => insertText(c.doc, 5, ' A'), (c) => insertText(c.doc, 5, ' B'));
    expect(['hello AB', 'hello BA']).toContain(lines(A.doc)[0].text);
    const { A: A2 } = race('hello', (c) => insertText(c.doc, 5, '-AAA'), (c) => insertText(c.doc, 5, '+BBB'));
    expect(['hello-AAA+BBB', 'hello+BBB-AAA']).toContain(lines(A2.doc)[0].text);
  });

  it('nothing useless travels when the edits differ', () => {
    const { relay, A, B } = seeded('hello');
    insertText(A.doc, 5, '-A');
    deleteRange(B.doc, 0, 1);
    relay.settle();
    expect(converged(A, B)).toBe(true);
    expect(A.useless + B.useless).toBe(0);
  });

  it('identical inserts collapse into one element', () => {
    const { A } = race('ab', (c) => insertText(c.doc, 2, 'x'), (c) => insertText(c.doc, 2, 'x'));
    expect(A.text()).toBe('abx\n');
  });

  it('deleteRange racing an insert inside it', () => {
    const { A } = race('hello world', (c) => deleteRange(c.doc, 6, 11), (c) => insertText(c.doc, 8, '[big]'));
    expect(A.text()).toBe('hello [big]\n');
  });

  it('overlapping deletes', () => {
    const { A } = race('abcdef', (c) => deleteRange(c.doc, 0, 3), (c) => deleteRange(c.doc, 1, 4));
    expect(A.text()).toBe('ef\n');
  });

  it('deleting everything on both sides leaves one terminated empty line', () => {
    const { A } = race('abc', (c) => deleteRange(c.doc, 0, 4), (c) => deleteRange(c.doc, 0, 4));
    expect(A.text()).toBe('\n');
  });

  it('splitLine at the same end is one Enter (identical ops collapse)', () => {
    const { A } = race('item', (c) => splitLine(c.doc, 4), (c) => splitLine(c.doc, 4));
    expect(A.text()).toBe('item\n\n');
  });

  it('splitLine in a heading vs typing at its end', () => {
    const { relay, A, B } = seeded('Hello world');
    setLineKind(A.doc, 0, 0, 'h2');
    relay.settle();
    splitLine(A.doc, 5);
    insertText(B.doc, 11, '!');
    relay.settle();
    expect(converged(A, B)).toBe(true);
    expect(A.view()).toBe('"Hello":h2\n" world!":-');
  });

  it('join vs typing on the joined line', () => {
    const { relay, A, B } = seeded('Head\nbody');
    setLineKind(A.doc, 0, 0, 'h2');
    setLineKind(A.doc, 5, 5, 'bullet');
    relay.settle();
    joinWithPrevious(A.doc, 5); // clears the bullet
    relay.settle();
    joinWithPrevious(A.doc, 5); // joins
    insertText(B.doc, 9, ' more');
    relay.settle();
    expect(converged(A, B)).toBe(true);
    expect(A.view()).toBe('"Headbody more":h2');
  });

  it('conflicting kinds resolve the same way on both', () => {
    const { A } = race('line', (c) => setLineKind(c.doc, 0, 0, 'h2'), (c) => setLineKind(c.doc, 0, 0, 'bullet'));
    const l = lines(A.doc)[0];
    expect(l.kinds.length).toBe(2);
    expect(l.kind).toBe(l.kinds[0]);
  });

  it('tick vs untick resolves to done', () => {
    const { relay, A, B } = seeded('todo');
    toggleTask(A.doc, 0);
    relay.settle();
    toggleTask(A.doc, 0); // done
    relay.settle();
    toggleTask(A.doc, 0); // A: task
    insertText(B.doc, 4, '!'); // B's untick is authored from a different state (a distinct op)
    toggleTask(B.doc, 0); // B: task; then A ticks again before seeing B
    toggleTask(A.doc, 0);
    relay.settle();
    expect(converged(A, B)).toBe(true);
    expect(lines(A.doc)[0].kinds.sort()).toEqual(['task', 'task_done']);
    expect(lines(A.doc)[0].kind).toBe('task_done');
  });

  it('toggleInline on overlapping ranges', () => {
    const { A } = race('abcdefgh', (c) => toggleInline(c.doc, 0, 5, 'bold'), (c) => toggleInline(c.doc, 3, 8, 'italic'));
    const runs = lines(A.doc)[0].runs.map((r) => [r.text, r.bold, r.italic]);
    expect(runs).toEqual([
      ['abc', true, false],
      ['de', true, true],
      ['fgh', false, true]
    ]);
  });

  it('bold vs concurrent unbold of the overlap keeps add-wins', () => {
    const { relay, A, B } = seeded('abcdef');
    toggleInline(A.doc, 0, 6, 'bold');
    relay.settle();
    toggleInline(A.doc, 2, 4, 'bold'); // off on "cd"
    toggleInline(B.doc, 0, 6, 'bold'); // off everywhere
    relay.settle();
    expect(converged(A, B)).toBe(true);
    expect(lines(A.doc)[0].runs.every((r) => !r.bold)).toBe(true);
  });

  it('setLink with a long URL reaches the other replica through artifact frames', () => {
    const url = 'https://example.com/a/very/long/path?with=query&and=more';
    const { relay, A, B } = seeded('see docs');
    const n = relay.log.length;
    setLink(A.doc, 4, 8, url);
    relay.settle();
    expect(relay.artifactFrames).toBe(1);
    expect(lines(B.doc)[0].runs.find((r) => r.text === 'docs')?.link).toBe(url);
    expect(converged(A, B)).toBe(true);
    // The artifact frame comes before the delta that needs it.
    expect(relay.log.slice(n).map((f) => f[0])).toEqual([0xaf, 0xde]);
  });

  it('concurrent links conflict, a later one settles it', () => {
    const { relay, A, B } = race('docs', (c) => setLink(c.doc, 0, 4, 'https://a.example.com/'), (c) => setLink(c.doc, 0, 4, 'https://b.example.com/'));
    expect(lines(A.doc)[0].runs[0].linkConflict?.length).toBe(2);
    setLink(A.doc, 0, 4, 'https://c.example.com/');
    relay.settle();
    expect(converged(A, B)).toBe(true);
    expect(lines(B.doc)[0].runs[0].link).toBe('https://c.example.com/');
  });

  it('comments added concurrently both survive, removal by id travels', () => {
    const { relay, A, B } = race('one two', (c) => addComment(c.doc, 0, 3, 'aaaaaaaaaaaa'), (c) => addComment(c.doc, 2, 7, 'bbbbbbbbbbbb'));
    expect(lines(A.doc)[0].runs.map((r) => r.comments.join('+'))).toEqual(['aaaaaaaaaaaa', 'aaaaaaaaaaaa+bbbbbbbbbbbb', 'bbbbbbbbbbbb']);
    removeComment(B.doc, 'aaaaaaaaaaaa');
    relay.settle();
    expect(converged(A, B)).toBe(true);
    expect(lines(A.doc)[0].runs.map((r) => r.comments.join('+'))).toEqual(['', 'bbbbbbbbbbbb']);
  });

  it('input rules applied on both sides, one racing a remote insert', () => {
    const { relay, A, B } = seeded('- ');
    const ruleA = inputRule('- ', 2, ' ', null);
    applyInputRule(A.doc, lines(A.doc)[0], ruleA as never);
    insertText(B.doc, 2, 'item');
    relay.settle();
    expect(converged(A, B)).toBe(true);
    expect(A.view()).toBe('"item":bullet');
    splitLine(B.doc, 4);
    insertText(B.doc, 5, '`x`');
    relay.settle();
    const l = lines(A.doc)[1];
    const rule = inputRule(l.text, 3, '`', l.kind);
    applyInputRule(A.doc, l, rule as never);
    applyInputRule(B.doc, lines(B.doc)[1], rule as never); // the same conversion on both
    relay.settle();
    expect(converged(A, B)).toBe(true);
    expect(A.view()).toBe('"item":bullet\n"x":bullet "x"{c}');
  });

  it('insertModel on both sides', () => {
    const model = { text: 'x\n', lines: [{ kind: 'bullet' as const }], inline: [] };
    const { A } = race('base', (c) => insertModel(c.doc, 0, model), (c) => insertModel(c.doc, 5, { ...model, text: 'y\n', lines: [{ kind: 'h3' as const }] }));
    expect(A.view()).toBe('"x":bullet\n"base":-\n"y":h3');
  });
});

describe('tables under concurrency', () => {
  const cellsOf = (c: Client, i: number) => lines(c.doc)[i].cells?.map((cell) => cell.map((r) => r.text).join(''));

  function table(): { relay: ReturnType<typeof pair>['relay']; A: Client; B: Client } {
    const { relay, A, B } = seeded('');
    insertTable(A.doc, 0, 2, 2);
    insertText(A.doc, 0, 'a');
    insertText(A.doc, 2, 'b');
    insertText(A.doc, 4, 'c');
    insertText(A.doc, 6, 'd');
    relay.settle();
    expect(cellsOf(B, 0)).toEqual(['a', 'b']);
    expect(cellsOf(B, 1)).toEqual(['c', 'd']);
    return { relay, A, B };
  }

  it('concurrent addColumn at the same index collapses into one column', () => {
    const { relay, A, B } = table();
    addColumn(A.doc, 0, 1);
    addColumn(B.doc, 0, 1);
    relay.settle();
    expect(converged(A, B)).toBe(true);
    expect(cellsOf(A, 0)).toEqual(['a', '', 'b']);
    expect(cellsOf(A, 1)).toEqual(['c', '', 'd']);
  });

  it('addColumn racing a cell edit leaves a ragged row that lines() pads', () => {
    const { relay, A, B } = table();
    addColumn(A.doc, 0, 2);
    insertText(B.doc, 5, 'X'); // "cX" in row 1 cell 0
    addColumn(B.doc, 0, 2);
    relay.settle();
    expect(converged(A, B)).toBe(true);
    // Row 0 got one separator (identical ops), row 1 got two (B's insert changed the state).
    expect(cellsOf(A, 0)).toEqual(['a', 'b', '', '']);
    expect(cellsOf(A, 1)).toEqual(['cX', 'd', '', '']);
    expect(lines(A.doc)[0].cells?.length).toBe(lines(A.doc)[1].cells?.length);
  });

  it('removeColumn vs typing in that column', () => {
    const { relay, A, B } = table();
    removeColumn(A.doc, 0, 1);
    insertText(B.doc, 3, 'bb');
    relay.settle();
    expect(converged(A, B)).toBe(true);
    // The typed text survives the column's deletion and lands in the cell that absorbed it.
    expect(cellsOf(A, 0)).toEqual(['abb']);
    expect(cellsOf(A, 1)).toEqual(['c']);
  });

  it('addRow, removeRow, setAlign and nextCell on both sides', () => {
    const { relay, A, B } = table();
    addRow(A.doc, 0);
    removeRow(B.doc, 4);
    setAlign(B.doc, 0, ['right', 'left']);
    relay.settle();
    expect(converged(A, B)).toBe(true);
    const ls = lines(A.doc);
    expect(ls.filter((l) => l.cells).length).toBe(2);
    expect(ls[0].align).toEqual(['right', 'left']);
    expect(ls[1].text).toBe(CELL_SEP);
    const tA = nextCell(A.doc, 3);
    const tB = nextCell(B.doc, 3);
    relay.settle();
    expect(converged(A, B)).toBe(true);
    expect(tA.caret).toBe(tB.caret);
  });

  it('removing the head row on one side while the other edits it', () => {
    const { relay, A, B } = table();
    removeRow(A.doc, 0);
    insertText(B.doc, 1, 'aa');
    relay.settle();
    expect(converged(A, B)).toBe(true);
    const ls = lines(A.doc);
    expect(ls[0].kind).toBe('table_head');
    expect(cellsOf(A, 0)).toEqual(['aac', 'd']); // B's text survives, joined into the next row
  });
});

describe('three replicas and a late joiner', () => {
  it('keep byte-equal stores after interleaved edits', () => {
    const { relay, A, B } = seeded('The quick brown fox');
    setLineKind(A.doc, 0, 0, 'h1');
    toggleInline(B.doc, 4, 9, 'bold');
    for (let i = 0; i < 3; i++) {
      insertText(A.doc, 19 + i, 'A');
      A.flush();
      insertText(B.doc, 0, 'B');
      B.flush();
    }
    relay.settle();
    expect(converged(A, B)).toBe(true);
    expect(lines(A.doc)[0].kind).toBe('h1');
    expect(lines(A.doc)[0].runs.find((r) => r.bold)?.text).toBe('quick');
  });
});
