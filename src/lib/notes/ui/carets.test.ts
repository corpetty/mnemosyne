import { describe, expect, it } from 'vitest';
import { pair, single } from '../test-harness.js';
import { CARET_COLORS, PALETTE, caretColor, caretOffsets, colorsFor, paletteAt } from './carets.js';

/** The selection a person at `[a, f]` (offsets) would send: the id of the character before each end. */
function selOf(c: { web: { seqIdAt(o: string, i: number): string | undefined }; doc: { obj: string } }, a: number, f: number) {
  const id = (n: number) => (n > 0 ? (c.web.seqIdAt(c.doc.obj, n - 1) ?? null) : null);
  return { a: id(a), f: id(f) };
}

describe('caretOffsets', () => {
  it('maps ids to the offsets after their characters; null is the start', () => {
    const A = single();
    A.web.textInsert(A.doc.obj, 0, 'hello world\n');
    expect(caretOffsets(A.doc, selOf(A, 5, 11))).toEqual({ anchor: 5, focus: 11 });
    expect(caretOffsets(A.doc, selOf(A, 0, 0))).toEqual({ anchor: 0, focus: 0 });
    expect(caretOffsets(A.doc, { a: null, f: selOf(A, 0, 3).f })).toEqual({ anchor: 0, focus: 3 });
  });

  it('follows the anchors through a remote insert', () => {
    const { relay, A, B } = pair();
    A.web.textInsert(A.doc.obj, 0, 'hello world\n');
    A.flush();
    relay.settle();
    // B has the caret after "hello", A types in front of it and inside the selection.
    const sel = selOf(B, 2, 8);
    expect(caretOffsets(B.doc, sel)).toEqual({ anchor: 2, focus: 8 });
    A.web.textInsert(A.doc.obj, 0, '>> ');
    A.flush();
    relay.settle();
    expect(B.text()).toBe('>> hello world\n');
    expect(caretOffsets(B.doc, sel)).toEqual({ anchor: 5, focus: 11 });
    A.web.textInsert(A.doc.obj, 7, 'XYZ'); // inside the selection ("he|XYZ|llo")
    A.flush();
    relay.settle();
    expect(caretOffsets(B.doc, sel)).toEqual({ anchor: 5, focus: 14 });
    expect(caretOffsets(A.doc, sel)).toEqual({ anchor: 5, focus: 14 });
  });

  it('follows the anchors through a remote delete before them', () => {
    const { relay, A, B } = pair();
    A.web.textInsert(A.doc.obj, 0, 'abcdef ghij\n');
    A.flush();
    relay.settle();
    const sel = selOf(B, 8, 11);
    A.web.textRemove(A.doc.obj, 0, 4); // "abcd"
    A.flush();
    relay.settle();
    expect(B.text()).toBe('ef ghij\n');
    expect(caretOffsets(B.doc, sel)).toEqual({ anchor: 4, focus: 7 });
  });

  it('hides a caret whose anchor character was deleted, on either end', () => {
    const { relay, A, B } = pair();
    A.web.textInsert(A.doc.obj, 0, 'abcdef\n');
    A.flush();
    relay.settle();
    const sel = selOf(B, 2, 5);
    A.web.textRemove(A.doc.obj, 4, 1); // "e", the character the focus follows
    A.flush();
    relay.settle();
    expect(B.text()).toBe('abcdf\n');
    expect(caretOffsets(B.doc, sel)).toBeNull();
    // the other end alone (anchor) is hidden the same way
    const sel2 = selOf(B, 2, 4);
    A.web.textRemove(A.doc.obj, 1, 1); // "b", which the anchor ("b" is the 2nd char) follows
    A.flush();
    relay.settle();
    expect(caretOffsets(B.doc, sel2)).toBeNull();
  });

  it('hides an id the document does not have, or one that is not an id at all', () => {
    const A = single();
    A.web.textInsert(A.doc.obj, 0, 'abc\n');
    expect(caretOffsets(A.doc, { a: 'f'.repeat(64), f: null })).toBeNull();
    expect(caretOffsets(A.doc, { a: null, f: 'nonsense' })).toBeNull();
    expect(caretOffsets(A.doc, { a: '', f: null })).toBeNull();
  });
});

describe('colours', () => {
  it('are stable: the same who always gets the same colour, in range', () => {
    for (const who of ['bob', 'u-17', '7', 'Corey', '', 'ünï😀']) {
      const c = caretColor(who);
      expect(Number.isInteger(c)).toBe(true);
      expect(c).toBeGreaterThanOrEqual(0);
      expect(c).toBeLessThan(CARET_COLORS);
      expect(caretColor(who)).toBe(c);
      expect(colorsFor(who)).toBe(PALETTE[c]);
    }
  });

  it('pins the hash (FNV-1a mod 8): changing it recolours everyone', () => {
    expect(['', 'a', 'bob', 'Corey', 'o:k1'].map(caretColor)).toEqual([5, 4, 4, 5, 4]);
  });

  it('spread people over the palette', () => {
    const used = new Set(Array.from({ length: 64 }, (_, i) => caretColor(`user-${i}`)));
    expect(used.size).toBe(CARET_COLORS);
  });

  it('give a palette of eight distinct colours with a translucent fill', () => {
    expect(PALETTE).toHaveLength(CARET_COLORS);
    expect(new Set(PALETTE.map((p) => p.bar)).size).toBe(CARET_COLORS);
    for (const p of PALETTE) {
      expect(p.bar).toMatch(/^#[0-9a-f]{6}$/);
      expect(p.fill).toMatch(/^#[0-9a-f]{8}$/);
    }
    expect(paletteAt(9)).toBe(PALETTE[1]);
    expect(paletteAt(-1)).toBe(PALETTE[7]);
  });
});
