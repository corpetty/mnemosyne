import { describe, expect, it } from 'vitest';
import { single, view, type Client } from './test-harness.js';
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
  prevCell,
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
import { CELL_SEP, ensureTerminated, lineAt, lines } from './model.js';

/** A doc with these lines (kind after a colon, e.g. "Title:h1"). */
function docOf(...spec: string[]): Client {
  const A = single();
  ensureTerminated(A.doc);
  let at = 0;
  for (const s of spec) {
    const m = /^(.*?)(?::([a-z_0-9]+))?$/s.exec(s) as RegExpExecArray;
    const [, text, kind] = m;
    insertText(A.doc, at, text);
    splitLine(A.doc, at + Array.from(text).length);
    if (kind) setLineKind(A.doc, at, at, kind as never);
    at += Array.from(text).length + 1;
  }
  // The trailing empty line the last splitLine made.
  deleteRange(A.doc, at - 1, at);
  return A;
}

/** Bytes the replica would send: 0 means the command was a no-op on the wire. */
const pending = (A: Client) => A.web.takeDeltas().length;

describe('text', () => {
  it('insertText inherits an expanding mark at a line end and keeps the doc terminated', () => {
    const A = docOf('hello world');
    toggleInline(A.doc, 6, 11, 'bold');
    const r = insertText(A.doc, 11, '!!');
    expect(r.caret).toBe(13);
    expect(A.view()).toBe('"hello world!!":- "world!!"{b}');
    insertText(A.doc, 0, '>');
    expect(A.view()).toBe('">hello world!!":- "world!!"{b}');
  });

  it('deleteRange keeps the kind of the line where the deletion starts', () => {
    const A = docOf('Head:h2', 'body:bullet', 'more');
    deleteRange(A.doc, 4, 5);
    expect(A.view()).toBe('"Headbody":h2\n"more":-');
    const B = docOf('Head:h2', 'body:bullet', 'more');
    deleteRange(B.doc, 2, 11); // "ad\nbody\nm"
    expect(B.view()).toBe('"Heore":h2');
  });

  it('deleteRange to the end of the doc keeps the first line kind', () => {
    const A = docOf('Head:h2', 'body:bullet');
    deleteRange(A.doc, 2, A.web.textLen(A.doc.obj));
    expect(A.view()).toBe('"He":h2');
  });

  it('deleteRange of everything leaves one empty line', () => {
    const A = docOf('a:h1', 'b');
    deleteRange(A.doc, 0, A.web.textLen(A.doc.obj));
    expect(A.text()).toBe('\n');
    expect(A.view()).toBe('"":-');
  });

  it('deleteRange within a line changes no kind', () => {
    const A = docOf('hello:quote');
    deleteRange(A.doc, 1, 3);
    expect(A.view()).toBe('"hlo":quote');
  });
});

describe('splitLine', () => {
  it('at the end of a heading: the heading stays, the new line is plain', () => {
    const A = docOf('Title:h1', 'after');
    const r = splitLine(A.doc, 5);
    expect(r.caret).toBe(6);
    expect(A.view()).toBe('"Title":h1\n"":-\n"after":-');
    insertText(A.doc, 6, 'typed');
    expect(A.view()).toBe('"Title":h1\n"typed":-\n"after":-');
  });

  it('at the end of a bold line: typing on the new line is plain', () => {
    const A = docOf('bold');
    toggleInline(A.doc, 0, 4, 'bold');
    splitLine(A.doc, 4);
    insertText(A.doc, 5, 'x');
    expect(A.view()).toBe('"bold":- "bold"{b}\n"x":-');
  });

  it('in the middle of a heading: the first half keeps the heading', () => {
    const A = docOf('Hello world:h2');
    const r = splitLine(A.doc, 5);
    expect(r.caret).toBe(6);
    expect(A.view()).toBe('"Hello":h2\n" world":-');
  });

  it('at the start of a heading pushes it down', () => {
    const A = docOf('Hello:h2');
    splitLine(A.doc, 0);
    expect(A.view()).toBe('"":-\n"Hello":h2');
  });

  it('continues lists, quotes and code; a ticked task continues unticked', () => {
    for (const [kind, next] of [
      ['bullet', 'bullet'],
      ['number', 'number'],
      ['task', 'task'],
      ['task_done', 'task'],
      ['quote', 'quote'],
      ['code', 'code']
    ]) {
      const A = docOf(`item:${kind}`);
      splitLine(A.doc, 4);
      expect(A.view(), kind).toBe(`"item":${kind}\n"":${next}`);
    }
  });

  it('in the middle of a bullet both halves are bullets', () => {
    const A = docOf('ab cd:bullet');
    splitLine(A.doc, 2);
    expect(A.view()).toBe('"ab":bullet\n" cd":bullet');
  });

  it('on an empty list line clears the kind', () => {
    const A = docOf('item:bullet', ':bullet', 'after');
    const r = splitLine(A.doc, 5);
    expect(r.caret).toBe(5);
    expect(A.view()).toBe('"item":bullet\n"":-\n"after":-');
  });

  it('moves lang to the first half of a split code line', () => {
    const A = docOf('let x:code');
    A.web.markRangeClosed(A.doc.obj, 5, 6, 'lang', 'rust');
    splitLine(A.doc, 3);
    expect(A.view()).toBe('"let":code lang=rust\n" x":code');
  });

  it('in a table adds a plain line after the row', () => {
    const A = docOf('x');
    insertTable(A.doc, 0, 2, 1);
    const r = splitLine(A.doc, 0);
    expect(r.caret).toBe(2);
    expect(view(lines(A.doc)).split('\n')[1]).toBe('"":-');
  });
});

describe('joinWithPrevious', () => {
  it('first clears the kind, then joins keeping the first line kind', () => {
    const A = docOf('Head:h2', 'item:bullet');
    let r = joinWithPrevious(A.doc, 5);
    expect(r.caret).toBe(5);
    expect(A.view()).toBe('"Head":h2\n"item":-');
    r = joinWithPrevious(A.doc, 5);
    expect(r.caret).toBe(4);
    expect(A.view()).toBe('"Headitem":h2');
  });

  it('is a no-op on the first plain line', () => {
    const A = docOf('only');
    const r = joinWithPrevious(A.doc, 0);
    expect(r.inverse).toEqual([]);
    expect(A.view()).toBe('"only":-');
  });

  it('off a line start deletes the previous character', () => {
    const A = docOf('abc');
    joinWithPrevious(A.doc, 2);
    expect(A.view()).toBe('"ac":-');
  });
});

describe('line kinds', () => {
  it('setLineKind marks every line the range touches and clears with null', () => {
    const A = docOf('a', 'b', 'c');
    setLineKind(A.doc, 0, 3, 'number');
    expect(A.view()).toBe('"a":number\n"b":number\n"c":-');
    setLineKind(A.doc, 2, 2, null);
    expect(A.view()).toBe('"a":number\n"b":-\n"c":-');
  });

  it('never emits a no-op mark', () => {
    const A = docOf('a:h1');
    pending(A);
    expect(setLineKind(A.doc, 0, 0, 'h1').inverse).toEqual([]);
    expect(pending(A)).toBe(0);
    expect(setLineKind(A.doc, 0, 0, 'h2').inverse.length).toBe(1);
    expect(pending(A)).toBeGreaterThan(0);
    setLineKind(A.doc, 0, 0, null);
    pending(A);
    expect(setLineKind(A.doc, 0, 0, null).inverse).toEqual([]);
    expect(pending(A)).toBe(0);
  });

  it('leaving code drops the lang', () => {
    const A = docOf('x:code');
    A.web.markRangeClosed(A.doc.obj, 1, 2, 'lang', 'py');
    setLineKind(A.doc, 0, 0, 'quote');
    expect(A.view()).toBe('"x":quote');
  });

  it('toggleTask ticks, unticks and makes a task of a plain line', () => {
    const A = docOf('buy milk');
    toggleTask(A.doc, 0);
    expect(A.view()).toBe('"buy milk":task');
    toggleTask(A.doc, 0);
    expect(A.view()).toBe('"buy milk":task_done');
    toggleTask(A.doc, 0);
    expect(A.view()).toBe('"buy milk":task');
  });
});

describe('inline', () => {
  it('toggleInline turns on, then off when fully on, partially on turns on everywhere', () => {
    const A = docOf('abcdef');
    toggleInline(A.doc, 0, 3, 'italic');
    expect(A.view()).toBe('"abcdef":- "abc"{i}');
    toggleInline(A.doc, 1, 5, 'italic');
    expect(A.view()).toBe('"abcdef":- "abcde"{i}');
    toggleInline(A.doc, 0, 5, 'italic');
    expect(A.view()).toBe('"abcdef":-');
    expect(toggleInline(A.doc, 2, 2, 'bold').inverse).toEqual([]);
  });

  it('strike expands, code is closed', () => {
    const A = docOf('ab cd');
    toggleInline(A.doc, 0, 2, 'strike');
    toggleInline(A.doc, 3, 5, 'code');
    insertText(A.doc, 2, 'X');
    insertText(A.doc, 6, 'Y');
    expect(A.view()).toBe('"abX cdY":- "abX"{s} "cd"{c}');
  });

  it('setLink sets, replaces and clears, with no-ops skipped', () => {
    const A = docOf('docs');
    setLink(A.doc, 0, 4, 'https://example.com/a');
    expect(A.view()).toBe('"docs":- "docs"{link=https://example.com/a}');
    pending(A);
    expect(setLink(A.doc, 0, 4, 'https://example.com/a').inverse).toEqual([]);
    expect(pending(A)).toBe(0);
    setLink(A.doc, 0, 4, 'https://example.com/b');
    expect(A.view()).toBe('"docs":- "docs"{link=https://example.com/b}');
    setLink(A.doc, 0, 4, null);
    expect(A.view()).toBe('"docs":-');
    expect(setLink(A.doc, 0, 4, null).inverse).toEqual([]);
  });

  it('comments coexist and are removed by id', () => {
    const A = docOf('one two three');
    addComment(A.doc, 0, 7, 'c1');
    addComment(A.doc, 4, 13, 'c2');
    expect(A.view()).toBe('"one two three":- "one "{#c1} "two"{#c1,#c2} " three"{#c2}');
    removeComment(A.doc, 'c1');
    expect(A.view()).toBe('"one two three":- "two three"{#c2}');
    expect(removeComment(A.doc, 'c1').inverse).toEqual([]);
  });
});

describe('tables', () => {
  it('insertTable before the current line, then rows, columns, alignment', () => {
    const A = docOf('intro', 'x');
    const r = insertTable(A.doc, 7, 3, 2);
    expect(r.caret).toBe(6);
    expect(A.view()).toBe(
      ['"intro":-', '"\\u001f\\u001f":table_head cells=["","",""]', '"\\u001f\\u001f":table cells=["","",""]', '"x":-'].join('\n')
    );
    insertText(A.doc, 6, 'a');
    insertText(A.doc, 8, 'b');
    insertText(A.doc, 10, 'c');
    addRow(A.doc, 6);
    expect(lines(A.doc)[2].cells?.length).toBe(3);
    expect(lines(A.doc)[2].kind).toBe('table');
    setAlign(A.doc, 6, ['left', 'center', 'right']);
    addColumn(A.doc, 6, 1);
    let ls = lines(A.doc);
    expect(ls[1].cells?.map((c) => c.map((x) => x.text).join(''))).toEqual(['a', '', 'b', 'c']);
    expect(ls[2].cells?.length).toBe(4);
    expect(ls[3].cells?.length).toBe(4);
    expect(ls[1].align).toEqual(['left', null, 'center', 'right']);
    removeColumn(A.doc, 6, 0);
    ls = lines(A.doc);
    expect(ls[1].cells?.map((c) => c.map((x) => x.text).join(''))).toEqual(['', 'b', 'c']);
    expect(ls[1].align).toEqual([null, 'center', 'right']);
    removeRow(A.doc, 6);
    ls = lines(A.doc);
    expect(ls[1].kind).toBe('table_head');
    expect(ls[1].align).toEqual([null, 'center', 'right']);
    expect(ls[1].cells?.length).toBe(3);
    removeRow(A.doc, 6);
    removeRow(A.doc, 6);
    expect(A.view()).toBe('"intro":-\n"x":-');
  });

  it('nextCell and prevCell move between cells and rows; Tab in the last cell adds a row', () => {
    const A = docOf('');
    insertTable(A.doc, 0, 2, 2);
    insertText(A.doc, 0, 'aa');
    insertText(A.doc, 3, 'bb');
    // Row 0: "aa\x1fbb\n" (0..5), row 1: "\x1f\n" (6..7).
    let t = nextCell(A.doc, 0);
    expect([t.caret, t.end]).toEqual([3, 5]);
    t = nextCell(A.doc, 4);
    expect([t.caret, t.end]).toEqual([6, 6]);
    t = nextCell(A.doc, 6);
    expect([t.caret, t.end]).toEqual([7, 7]);
    t = nextCell(A.doc, 7);
    expect(lines(A.doc).filter((l) => l.cells).length).toBe(3);
    expect(t.caret).toBe(8);
    expect(t.inverse.length).toBe(1);
    t = prevCell(A.doc, 8);
    expect([t.caret, t.end]).toEqual([7, 7]);
    t = prevCell(A.doc, 7);
    expect([t.caret, t.end]).toEqual([6, 6]);
    t = prevCell(A.doc, 6);
    expect([t.caret, t.end]).toEqual([3, 5]);
    t = prevCell(A.doc, 3);
    expect([t.caret, t.end]).toEqual([0, 2]);
    t = prevCell(A.doc, 0);
    expect(t.caret).toBe(0);
  });
});

describe('insertModel', () => {
  it('takes only its own inline marks, not an expanding one ending at the insertion point', () => {
    const A = docOf('ab');
    toggleInline(A.doc, 0, 2, 'bold');
    const r = insertModel(A.doc, 2, { text: 'cd', lines: [], inline: [{ from: 1, to: 2, kind: 'italic' }] });
    expect(r.caret).toBe(4);
    expect(A.view()).toBe('"abcd":- "ab"{b} "d"{i}');
    insertText(A.doc, 2, 'x'); // typing after the bold still extends it
    expect(A.view()).toBe('"abxcd":- "abx"{b} "d"{i}');
  });

  it('pastes lines with kinds and inline marks', () => {
    const A = docOf('before', 'after:quote');
    const r = insertModel(A.doc, 7, {
      text: 'Head\nlist 😀 item\nlet x\n',
      lines: [{ kind: 'h2' }, { kind: 'bullet' }, { kind: 'code', lang: 'js' }],
      inline: [
        { from: 5, to: 9, kind: 'bold' },
        { from: 12, to: 16, kind: 'link', value: 'https://example.com/paste' }
      ]
    });
    expect(r.caret).toBe(7 + 23);
    expect(A.view()).toBe(
      ['"before":-', '"Head":h2', '"list 😀 item":bullet "list"{b} "item"{link=https://example.com/paste}', '"let x":code lang=js', '"after":quote'].join('\n')
    );
  });
});

describe('input rules', () => {
  it('converts typed prefixes on plain lines only', () => {
    expect(inputRule('# ', 2, ' ', null)).toEqual({ type: 'kind', kind: 'h1', prefix: 2 });
    expect(inputRule('### ', 4, ' ', null)).toEqual({ type: 'kind', kind: 'h3', prefix: 4 });
    expect(inputRule('- ', 2, ' ', null)).toEqual({ type: 'kind', kind: 'bullet', prefix: 2 });
    expect(inputRule('* ', 2, ' ', null)).toEqual({ type: 'kind', kind: 'bullet', prefix: 2 });
    expect(inputRule('12. ', 4, ' ', null)).toEqual({ type: 'kind', kind: 'number', prefix: 4 });
    expect(inputRule('> ', 2, ' ', null)).toEqual({ type: 'kind', kind: 'quote', prefix: 2 });
    expect(inputRule('[ ] ', 4, ' ', null)).toEqual({ type: 'kind', kind: 'task', prefix: 4 });
    expect(inputRule('- [x] ', 6, ' ', null)).toEqual({ type: 'kind', kind: 'task_done', prefix: 6 });
    expect(inputRule('[ ] ', 4, ' ', 'bullet')).toEqual({ type: 'kind', kind: 'task', prefix: 4 });
    expect(inputRule('# ', 2, ' ', 'h2')).toBeNull();
    expect(inputRule('a # ', 4, ' ', null)).toBeNull();
    expect(inputRule('# x ', 4, ' ', null)).toBeNull();
  });

  it('closes inline code on the second backtick', () => {
    expect(inputRule('use `foo`', 9, '`', null)).toEqual({ type: 'code', open: 4, close: 8 });
    expect(inputRule('use ``', 6, '`', null)).toBeNull();
    expect(inputRule('use `foo`', 9, '`', 'code')).toBeNull();
    expect(inputRule('😀 `x`', 5, '`', null)).toEqual({ type: 'code', open: 2, close: 4 });
  });

  it('fences and tables on Enter', () => {
    expect(inputRule('```rust', 7, '\n', null)).toEqual({ type: 'fence', lang: 'rust' });
    expect(inputRule('```', 3, '\n', null)).toEqual({ type: 'fence', lang: '' });
    expect(inputRule('```', 3, '\n', 'code')).toEqual({ type: 'endFence' });
    expect(inputRule('let x', 5, '\n', 'code')).toBeNull();
    expect(inputRule('|a| b |', 7, '\n', null)).toEqual({ type: 'table', cells: ['a', 'b'] });
    expect(inputRule('|a', 2, '\n', null)).toBeNull();
  });

  it('applies each conversion', () => {
    const A = docOf('x', '## ', 'y');
    let ls = lines(A.doc);
    let r = applyInputRule(A.doc, ls[1], inputRule(ls[1].text, 3, ' ', null) as never);
    expect(r.caret).toBe(2);
    expect(A.view()).toBe('"x":-\n"":h2\n"y":-');

    const B = docOf('see `code` here');
    ls = lines(B.doc);
    r = applyInputRule(B.doc, ls[0], inputRule(ls[0].text, 10, '`', null) as never);
    expect(r.caret).toBe(8);
    expect(B.view()).toBe('"see code here":- "code"{c}');
    insertText(B.doc, 8, 'd');
    expect(B.view()).toBe('"see coded here":- "code"{c}');

    const C = docOf('```py', 'after');
    ls = lines(C.doc);
    r = applyInputRule(C.doc, ls[0], { type: 'fence', lang: 'py' });
    expect(r.caret).toBe(0);
    expect(C.view()).toBe('"":code lang=py\n"after":-');
    insertText(C.doc, 0, 'x = 1');
    splitLine(C.doc, 5);
    insertText(C.doc, 6, '```');
    ls = lines(C.doc);
    expect(inputRule(ls[1].text, 3, '\n', ls[1].kind)).toEqual({ type: 'endFence' });
    applyInputRule(C.doc, ls[1], { type: 'endFence' });
    expect(C.view()).toBe('"x = 1":code lang=py\n"":-\n"after":-');

    const D = docOf('|a|b|', 'z');
    ls = lines(D.doc);
    r = applyInputRule(D.doc, ls[0], inputRule(ls[0].text, 5, '\n', null) as never);
    ls = lines(D.doc);
    expect(ls[0].kind).toBe('table_head');
    expect(ls[0].cells?.map((c) => c.map((x) => x.text).join(''))).toEqual(['a', 'b']);
    expect(ls[1].kind).toBe('table');
    expect(ls[1].cells?.length).toBe(2);
    expect(r.caret).toBe(ls[1].start);
    expect(ls[2].text).toBe('z');
  });
});

describe('emoji offsets', () => {
  it('every command indexes code points', () => {
    const A = docOf('a😀b');
    toggleInline(A.doc, 1, 2, 'bold');
    expect(A.view()).toBe('"a😀b":- "😀"{b}');
    splitLine(A.doc, 2);
    expect(A.view()).toBe('"a😀":- "😀"{b}\n"b":-');
    deleteRange(A.doc, 1, 2);
    expect(A.view()).toBe('"a":-\n"b":-');
    insertText(A.doc, 1, '👨‍👩‍👧');
    expect(lineAt(lines(A.doc), 1).text).toBe('a👨‍👩‍👧');
    expect(lines(A.doc)[1].start).toBe(7);
  });
});
