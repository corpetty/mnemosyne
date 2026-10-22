import { describe, expect, it } from 'vitest';
import { single, pair } from './test-harness.js';
import {
  BLOCK,
  CELL_SEP,
  cpIndex,
  cpLen,
  cpSlice,
  decodeAlign,
  encodeAlign,
  ensureTerminated,
  lineAt,
  lines,
  resolveKind,
  utf16Index
} from './model.js';

describe('code points vs UTF-16', () => {
  const s = 'x😀y\n';
  it('counts code points', () => {
    expect(s.length).toBe(5);
    expect(cpLen(s)).toBe(4);
  });
  it('maps offsets both ways', () => {
    expect(utf16Index(s, 2)).toBe(3);
    expect(utf16Index(s, 9)).toBe(5);
    expect(cpIndex(s, 3)).toBe(2);
    expect(cpIndex(s, 2)).toBe(2); // mid-surrogate rounds up
    expect(cpSlice(s, 1, 3)).toBe('😀y');
  });
  it('agrees with the wasm index unit', () => {
    const A = single();
    A.web.textInsert(A.doc.obj, 0, s);
    expect(A.web.textLen(A.doc.obj)).toBe(cpLen(s));
    A.web.textInsert(A.doc.obj, 2, 'Z');
    expect(A.text()).toBe('x😀Zy\n');
    const ls = lines(A.doc);
    expect(ls[0].text).toBe('x😀Zy');
    expect(ls[0].end).toBe(4);
  });
});

describe('resolveKind', () => {
  it('ticks win over unticks', () => {
    expect(resolveKind(['task', 'task_done'])).toBe('task_done');
    expect(resolveKind(['task_done', 'task'])).toBe('task_done');
  });
  it('otherwise takes the first known value', () => {
    expect(resolveKind(['bullet', 'h2'])).toBe('bullet');
    expect(resolveKind(['nonsense', 'h2'])).toBe('h2');
    expect(resolveKind([])).toBeNull();
  });
});

describe('lines()', () => {
  it('reads kinds, terminator ids, runs and extra values', () => {
    const A = single();
    const obj = A.doc.obj;
    A.web.textInsert(obj, 0, 'Title\nsome bold text\ncode\nplain\n');
    A.web.markRangeClosed(obj, 5, 6, BLOCK, 'h1');
    A.web.markRange(obj, 11, 15, 'bold', 'true');
    A.web.markRangeClosed(obj, 25, 26, BLOCK, 'code');
    A.web.markRangeClosed(obj, 25, 26, 'lang', 'rust');
    A.web.markRangeClosed(obj, 6, 10, 'link', 'https://example.com/long/enough/path');
    const ls = lines(A.doc);
    expect(ls.map((l) => l.text)).toEqual(['Title', 'some bold text', 'code', 'plain']);
    expect(ls.map((l) => l.kind)).toEqual(['h1', null, 'code', null]);
    expect(ls[2].lang).toBe('rust');
    expect(ls[1].runs.map((r) => [r.text, r.bold, r.link])).toEqual([
      ['some', false, 'https://example.com/long/enough/path'],
      [' ', false, null],
      ['bold', true, null],
      [' text', false, null]
    ]);
    for (const l of ls) {
      expect(l.id).toBeTruthy();
      expect(A.web.seqPositionOf(obj, l.id as string)).toBe(l.end);
    }
    expect(ls[1].start).toBe(6);
    expect(ls[1].end).toBe(20);
    expect(lineAt(ls, 20)).toBe(ls[1]);
    expect(lineAt(ls, 21)).toBe(ls[2]);
    expect(lineAt(ls, 0)).toBe(ls[0]);
  });

  it('surfaces a link conflict and comments', () => {
    const { relay, A, B } = pair();
    A.web.textInsert(A.doc.obj, 0, 'see here\n');
    relay.settle();
    A.web.markRangeClosed(A.doc.obj, 4, 8, 'link', 'https://a.example.com/x');
    B.web.markRangeClosed(B.doc.obj, 4, 8, 'link', 'https://b.example.com/y');
    A.web.markRangeClosed(A.doc.obj, 0, 3, 'comment:0123456789ab', 'true');
    relay.settle();
    for (const c of [A, B]) {
      const run = lines(c.doc)[0].runs.find((r) => r.text === 'here');
      expect(run?.link).toBeNull();
      expect(run?.linkConflict?.length).toBe(2);
      expect(lines(c.doc)[0].runs[0].comments).toEqual(['0123456789ab']);
    }
  });

  it('splits table rows into cells and pads ragged rows', () => {
    const A = single();
    const obj = A.doc.obj;
    const text = `name${CELL_SEP}age\nann${CELL_SEP}30${CELL_SEP}extra\nbob\nafter\n`;
    A.web.textInsert(obj, 0, text);
    const nl = (n: number) => Array.from(text).reduce<number[]>((acc, ch, i) => (ch === '\n' ? [...acc, i] : acc), [])[n];
    A.web.markRangeClosed(obj, nl(0), nl(0) + 1, BLOCK, 'table_head');
    A.web.markRangeClosed(obj, nl(0), nl(0) + 1, 'align', 'lr');
    A.web.markRangeClosed(obj, nl(1), nl(1) + 1, BLOCK, 'table');
    A.web.markRangeClosed(obj, nl(2), nl(2) + 1, BLOCK, 'table');
    const ls = lines(A.doc);
    const cells = (i: number) => ls[i].cells?.map((c) => c.map((r) => r.text).join(''));
    expect(cells(0)).toEqual(['name', 'age', '']);
    expect(cells(1)).toEqual(['ann', '30', 'extra']);
    expect(cells(2)).toEqual(['bob', '', '']);
    expect(ls[3].cells).toBeNull();
    expect(ls[0].align).toEqual(['left', 'right']);
    expect(ls[0].cellStarts).toEqual([0, 5, ls[0].end]);
    expect(ls[1].cellStarts).toEqual([9, 13, 16]);
  });

  it('reports an unterminated last line and ensureTerminated fixes it', () => {
    const A = single();
    expect(lines(A.doc)).toEqual([]);
    expect(ensureTerminated(A.doc)).toBe(true);
    expect(A.text()).toBe('\n');
    expect(ensureTerminated(A.doc)).toBe(false);
    A.web.textInsert(A.doc.obj, 1, 'tail');
    const ls = lines(A.doc);
    expect(ls.length).toBe(2);
    expect(ls[1].id).toBeNull();
    expect(ls[1].text).toBe('tail');
    expect(ensureTerminated(A.doc)).toBe(true);
    expect(lines(A.doc)[1].id).toBeTruthy();
  });
});

describe('align values', () => {
  it('round-trips one letter per column', () => {
    const a = ['left', null, 'center', 'right'] as const;
    expect(encodeAlign(a)).toBe('l-cr');
    expect(decodeAlign('l-cr')).toEqual([...a]);
  });
});
