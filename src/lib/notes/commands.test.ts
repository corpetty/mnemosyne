import { describe, expect, it } from 'vitest';
import { single, view, type Client } from './test-harness.js';
import {
  addColumn,
  addComment,
  addRow,
  applyInputRule,
  deleteRange,
  inputRule,
  insertImageLine,
  insertModel,
  insertTable,
  insertText,
  joinWithPrevious,
  nextCell,
  prevCell,
  removeColumn,
  removeComment,
  removeImageLine,
  removeRow,
  setAlign,
  setLineKind,
  setLink,
  splitLine,
  toggleInline,
  toggleTask
} from './commands.js';
import { CELL_SEP, IMAGE, ensureTerminated, lineAt, lines, spansOf, valuesAt } from './model.js';
import { parsePaste, selectionMarkdown, selectionModel } from './editor/clipboard.js';
import { UndoStack } from './undo.js';

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

describe('image lines', () => {
  const V = '3f0c5aa1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5ca9.webp@1600x900';
  const W = '00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff.jpeg@640x480';

  /** One entry per line: `[V]caption` for an image line, else `text:kind`. */
  const shape = (A: Client) =>
    lines(A.doc)
      .map((l) => (l.image ? `[${l.image === V ? 'V' : l.image === W ? 'W' : '?'}]${l.text}` : `${l.text}:${l.kind ?? '-'}`))
      .join(' | ');

  /** docOf, then line `i` made an image line showing `value`. */
  function withImage(i: number, value: string, ...spec: string[]): Client {
    const A = docOf(...spec);
    const end = lines(A.doc)[i].end;
    A.web.markRangeClosed(A.doc.obj, end, end + 1, 'block', 'image');
    A.web.markRangeClosed(A.doc.obj, end, end + 1, IMAGE, value);
    return A;
  }

  const imageMarks = (A: Client) => {
    const spans = spansOf(A.doc);
    return lines(A.doc).map((l) => valuesAt(spans, l.end, IMAGE).length);
  };

  describe('insertImageLine', () => {
    it('turns an empty host line into the image line', () => {
      const A = docOf('above:h2', '', 'below:bullet');
      const r = insertImageLine(A.doc, 6, V);
      expect(r.caret).toBe(6);
      expect(shape(A)).toBe('above:h2 | [V] | below:bullet');
    });

    it('adds a line after the caret line, or before it at a line start', () => {
      const A = docOf('one', 'two:h1');
      let r = insertImageLine(A.doc, 1, V);
      expect(shape(A)).toBe('one:- | [V] | two:h1');
      expect(r.caret).toBe(4);
      r = insertImageLine(A.doc, lines(A.doc)[2].start, W);
      expect(shape(A)).toBe('one:- | [V] | [W] | two:h1');
      expect(r.caret).toBe(5);
      // At the end of an image line's (empty) caption: after it.
      insertImageLine(A.doc, 4, W);
      expect(shape(A)).toBe('one:- | [V] | [W] | [W] | two:h1');
    });

    it('goes before or after a whole table, never between its rows', () => {
      const A = docOf('x');
      insertTable(A.doc, 0, 2, 2);
      insertImageLine(A.doc, lines(A.doc)[1].start, V);
      expect(shape(A).split(' | ').map((s) => s.replace(CELL_SEP, '|'))).toEqual(['|:table_head', '|:table', '[V]', 'x:-']);
      insertImageLine(A.doc, 0, W);
      expect(shape(A).startsWith('[W] | ')).toBe(true);
    });

    it('refuses an invalid value and undoes in one step', () => {
      const A = docOf('one');
      expect(insertImageLine(A.doc, 0, 'x.png@1x1').inverse).toEqual([]);
      expect(insertImageLine(A.doc, 0, V.toUpperCase()).inverse).toEqual([]);
      expect(shape(A)).toBe('one:-');
      const undo = new UndoStack(A.doc);
      undo.push(insertImageLine(A.doc, 3, V).inverse);
      expect(shape(A)).toBe('one:- | [V]');
      undo.undo();
      expect(shape(A)).toBe('one:-');
      undo.redo();
      expect(shape(A)).toBe('one:- | [V]');
    });
  });

  describe('Enter', () => {
    it('anywhere in the caption adds a plain line after the image, never splits', () => {
      for (const at of [0, 2, 7]) {
        const A = withImage(0, V, 'caption', 'next:h2');
        const r = splitLine(A.doc, at);
        expect(shape(A), `at ${at}`).toBe('[V]caption | :- | next:h2');
        expect(r.caret).toBe(8);
      }
    });
  });

  describe('Backspace', () => {
    it('at the start of an empty caption removes the whole line; one undo restores it', () => {
      const A = withImage(1, V, 'Head:h2', '', 'item:bullet');
      const undo = new UndoStack(A.doc);
      const r = joinWithPrevious(A.doc, 5);
      undo.push(r.inverse);
      expect(shape(A)).toBe('Head:h2 | item:bullet');
      expect(imageMarks(A)).toEqual([0, 0]);
      expect(r.caret).toBe(4);
      undo.undo();
      expect(shape(A)).toBe('Head:h2 | [V] | item:bullet');
    });

    it('at the start of a caption does nothing', () => {
      const A = withImage(1, V, 'Head:h2', 'cap', 'item');
      const r = joinWithPrevious(A.doc, 5);
      expect(r).toEqual({ inverse: [], caret: 5 });
      expect(shape(A)).toBe('Head:h2 | [V]cap | item:-');
    });

    it('below an image never joins into the caption; an empty line goes', () => {
      const A = withImage(0, V, 'cap', 'text:bullet', '', 'end');
      // First the kind, then nothing joins: the caret moves to the caption's end.
      joinWithPrevious(A.doc, 4);
      expect(shape(A)).toBe('[V]cap | text:- | :- | end:-');
      const r = joinWithPrevious(A.doc, 4);
      expect(r).toEqual({ inverse: [], caret: 3 });
      expect(shape(A)).toBe('[V]cap | text:- | :- | end:-');
      // An empty line below an image is removed.
      const B = withImage(0, V, 'cap', '', 'end');
      expect(joinWithPrevious(B.doc, 4).caret).toBe(3);
      expect(shape(B)).toBe('[V]cap | end:-');
      expect(B.text().endsWith('\n')).toBe(true);
    });

    it('removes the only line and keeps the doc terminated', () => {
      const A = withImage(0, V, '');
      joinWithPrevious(A.doc, 0);
      expect(A.text()).toBe('\n');
      expect(shape(A)).toBe(':-');
      expect(imageMarks(A)).toEqual([0]);
    });
  });

  describe('Delete', () => {
    it('at the end of the line above an image joins nothing; an empty line goes', () => {
      const A = withImage(1, V, 'above:h2', 'cap');
      expect(deleteRange(A.doc, 5, 6)).toEqual({ inverse: [], caret: 5 });
      expect(shape(A)).toBe('above:h2 | [V]cap');
      const B = withImage(1, V, '', 'cap');
      deleteRange(B.doc, 0, 1);
      expect(shape(B)).toBe('[V]cap');
    });

    it('at the end of a caption joins nothing; an empty line below goes', () => {
      const A = withImage(0, V, 'cap', 'below:h1');
      expect(deleteRange(A.doc, 3, 4).inverse).toEqual([]);
      expect(shape(A)).toBe('[V]cap | below:h1');
      const B = withImage(0, V, 'cap', '', 'below:h1');
      deleteRange(B.doc, 3, 4);
      expect(shape(B)).toBe('[V]cap | below:h1');
    });

    it('between two image lines joins nothing', () => {
      const A = withImage(0, V, 'a', 'b');
      const end = lines(A.doc)[1].end;
      A.web.markRangeClosed(A.doc.obj, end, end + 1, 'block', 'image');
      A.web.markRangeClosed(A.doc.obj, end, end + 1, IMAGE, W);
      expect(deleteRange(A.doc, 1, 2).inverse).toEqual([]);
      expect(shape(A)).toBe('[V]a | [W]b');
    });
  });

  describe('deleteRange across an image line', () => {
    it('into a caption: the image stays where it is and loses the selected head', () => {
      const A = withImage(2, V, 'above:h2', 'mid:bullet', 'caption', 'below');
      const undo = new UndoStack(A.doc);
      // "ove\nmid\nca"
      const r = deleteRange(A.doc, 2, 12);
      undo.push(r.inverse);
      expect(r.caret).toBe(2);
      expect(shape(A)).toBe('ab:h2 | [V]ption | below:-');
      undo.undo();
      expect(shape(A)).toBe('above:h2 | mid:bullet | [V]caption | below:-');
      // From the start of the line above, the caption head goes and the line above stays.
      deleteRange(A.doc, 0, 12);
      expect(shape(A)).toBe(':h2 | [V]ption | below:-');
    });

    it('from a caption: the image keeps its place and takes the rest of the last line', () => {
      const A = withImage(0, V, 'caption', 'below:h1', 'end');
      deleteRange(A.doc, 3, 11); // "tion\nbel"
      expect(shape(A)).toBe('[V]capow | end:-');
      const B = withImage(0, V, 'caption', 'below:h1');
      deleteRange(B.doc, 0, 11);
      expect(shape(B)).toBe('[V]ow');
    });

    it('over the whole image line removes it', () => {
      const A = withImage(1, V, 'above', 'caption', 'below:h1');
      deleteRange(A.doc, 3, 16); // "ve\ncaption\nbe"
      expect(shape(A)).toBe('abolow:-');
      expect(imageMarks(A)).toEqual([0]);
    });

    it('to the end of the doc keeps the image of the first line', () => {
      const A = withImage(0, V, 'caption', 'below');
      deleteRange(A.doc, 3, A.web.textLen(A.doc.obj));
      expect(shape(A)).toBe('[V]cap');
    });
  });

  describe('line kinds', () => {
    it('setLineKind over an image line keeps the caption as text and drops the picture', () => {
      const A = withImage(0, V, 'caption', 'b');
      setLineKind(A.doc, 0, 9, 'bullet');
      expect(shape(A)).toBe('caption:bullet | b:bullet');
      expect(imageMarks(A)).toEqual([0, 0]);
      const B = withImage(0, V, 'caption');
      setLineKind(B.doc, 0, 0, null);
      expect(shape(B)).toBe('caption:-');
      expect(imageMarks(B)).toEqual([0]);
    });

    it('never sets image without a value, never toggles a task on an image line', () => {
      const A = withImage(0, V, 'caption', 'b');
      expect(setLineKind(A.doc, 8, 8, 'image').inverse).toEqual([]);
      expect(toggleTask(A.doc, 0).inverse).toEqual([]);
      expect(shape(A)).toBe('[V]caption | b:-');
    });

    it('joinWithPrevious on a line that is not an image still clears an image mark with the kind', () => {
      const A = docOf('a', 'b:bullet');
      A.web.markRangeClosed(A.doc.obj, 3, 4, IMAGE, V);
      joinWithPrevious(A.doc, 2);
      expect(imageMarks(A)).toEqual([0, 0]);
    });
  });

  describe('removeImageLine', () => {
    it('removes the line its id names, whatever the offsets say now', () => {
      const A = withImage(1, V, 'a:h1', 'cap', 'b:quote');
      const line = lines(A.doc)[1];
      insertText(A.doc, 0, 'xyz');
      const r = removeImageLine(A.doc, line);
      expect(shape(A)).toBe('xyza:h1 | b:quote');
      expect(r.caret).toBe(4);
      expect(imageMarks(A)).toEqual([0, 0]);
    });

    it('does nothing for a line that is not an image line (any more)', () => {
      const A = withImage(0, V, 'cap', 'b');
      const line = lines(A.doc)[0];
      setLineKind(A.doc, 0, 0, null);
      expect(removeImageLine(A.doc, line).inverse).toEqual([]);
      expect(removeImageLine(A.doc, lines(A.doc)[1]).inverse).toEqual([]);
      expect(shape(A)).toBe('cap:- | b:-');
    });
  });

  describe('paste and copy', () => {
    it('insertModel marks image lines, an invalid one becomes a paragraph', () => {
      const A = docOf('before');
      insertModel(A.doc, 7, {
        text: 'cap\nbad\n',
        lines: [
          { kind: 'image', image: V },
          { kind: 'image', image: 'nope' }
        ],
        inline: []
      });
      expect(shape(A)).toBe('before:- | [V]cap | bad:-');
    });

    it('a selection with an image line round-trips through markdown', () => {
      const A = withImage(1, V, 'Head:h2', 'The board', 'after');
      const ls = lines(A.doc);
      const model = selectionModel(ls, 0, A.web.textLen(A.doc.obj));
      expect(model.lines[1]).toEqual({ kind: 'image', image: V });
      const md = selectionMarkdown(ls, 0, A.web.textLen(A.doc.obj));
      expect(md).toBe(`## Head\n![The board](mnemosyne:image/${V})\nafter\n`);
      const plan = parsePaste(md);
      expect(plan.kind).toBe('block');
      const B = single();
      ensureTerminated(B.doc);
      insertModel(B.doc, 0, plan.model);
      deleteRange(B.doc, B.web.textLen(B.doc.obj) - 1, B.web.textLen(B.doc.obj));
      expect(shape(B)).toBe(shape(A));
      // A lone image line is a block paste too.
      expect(parsePaste(`![](mnemosyne:image/${W})`).kind).toBe('block');
    });
  });
});
