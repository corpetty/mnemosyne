import { describe, expect, it } from 'vitest';
import { insertChip, formatClock, isChipUrl, meetingUrl, parseChip, personUrl, timeUrl } from './chips.js';
import { spansOf, ensureTerminated } from './model.js';
import { insertText } from './commands.js';
import { single } from './test-harness.js';
import { UndoStack } from './undo.js';

describe('chip urls', () => {
  it('builds and parses a time chip', () => {
    expect(timeUrl(0, 75)).toBe('mnemosyne:t/0/75');
    expect(timeUrl(2, 75.9)).toBe('mnemosyne:t/2/75');
    expect(timeUrl(-1, Number.NaN)).toBe('mnemosyne:t/0/0');
    expect(parseChip('mnemosyne:t/2/75')).toEqual({ kind: 'time', part: 2, seconds: 75 });
  });

  it('percent-encodes a person and decodes it back', () => {
    const url = personUrl('Ana María / O\'Neil 😀');
    expect(url).toBe(`mnemosyne:person/${encodeURIComponent('Ana María / O\'Neil 😀')}`);
    expect(url.slice('mnemosyne:person/'.length)).not.toContain('/');
    expect(parseChip(url)).toEqual({ kind: 'person', name: 'Ana María / O\'Neil 😀' });
  });

  it('builds and parses a meeting chip', () => {
    expect(meetingUrl('0123abcd4567')).toBe('mnemosyne:meeting/0123abcd4567');
    expect(parseChip('mnemosyne:meeting/0123abcd4567')).toEqual({ kind: 'meeting', id: '0123abcd4567' });
    expect(parseChip(`mnemosyne:meeting/${'a'.repeat(8)}`)).not.toBeNull();
    expect(parseChip(`mnemosyne:meeting/${'a'.repeat(32)}`)).not.toBeNull();
  });

  it.each([
    '',
    'https://example.com',
    'javascript:alert(1)',
    'mnemosyne:',
    'mnemosyne:t/1',
    'mnemosyne:t/1/2/3',
    'mnemosyne:t/-1/2',
    'mnemosyne:t/1/2.5',
    'mnemosyne:t/a/2',
    'mnemosyne:t/1/2 ',
    ' mnemosyne:t/1/2',
    'MNEMOSYNE:t/1/2',
    'mnemosyne:t/99999999999999999999/1',
    'mnemosyne:person/',
    'mnemosyne:person/a/b',
    'mnemosyne:person/%E0%A4%A',
    'mnemosyne:meeting/abc',
    'mnemosyne:meeting/ABCDEF0123',
    'mnemosyne:meeting/xyz12345',
    `mnemosyne:meeting/${'a'.repeat(33)}`,
    'mnemosyne:meeting/0123abcd/extra',
    'mnemosyne:other/x',
    'mnemosyne:t/1/2\n'
  ])('rejects %j', (url) => {
    expect(parseChip(url)).toBeNull();
    expect(isChipUrl(url)).toBe(false);
  });

  it('isChipUrl accepts the three forms', () => {
    expect(isChipUrl('mnemosyne:t/0/0')).toBe(true);
    expect(isChipUrl(personUrl('Bo'))).toBe(true);
    expect(isChipUrl(meetingUrl('deadbeef'))).toBe(true);
  });
});

describe('formatClock', () => {
  it('prints m:ss, then h:mm:ss from an hour', () => {
    expect(formatClock(0)).toBe('0:00');
    expect(formatClock(5)).toBe('0:05');
    expect(formatClock(75)).toBe('1:15');
    expect(formatClock(599.9)).toBe('9:59');
    expect(formatClock(3599)).toBe('59:59');
    expect(formatClock(3600)).toBe('1:00:00');
    expect(formatClock(3725)).toBe('1:02:05');
    expect(formatClock(36000)).toBe('10:00:00');
  });
  it('clamps nonsense to 0:00', () => {
    expect(formatClock(-4)).toBe('0:00');
    expect(formatClock(Number.NaN)).toBe('0:00');
  });
});

describe('insertChip', () => {
  const linkAt = (A: ReturnType<typeof single>, i: number) => spansOf(A.doc).find((s) => s.start <= i && i < s.end)?.marks.get('link') ?? [];

  it('inserts linked text and a trailing space, in one entry', () => {
    const A = single();
    ensureTerminated(A.doc);
    insertText(A.doc, 0, 'ab');
    const undo = new UndoStack(A.doc, { now: () => 0, groupMs: 1000 });
    const r = insertChip(A.doc, 1, '1:15', timeUrl(0, 75));
    undo.push(r.inverse, { caret: 1 });
    expect(A.text()).toBe('a1:15 b\n');
    expect(r.caret).toBe(6);
    expect(linkAt(A, 1)).toEqual(['mnemosyne:t/0/75']);
    expect(linkAt(A, 4)).toEqual(['mnemosyne:t/0/75']);
    expect(linkAt(A, 5)).toEqual([]); // the space is outside the link
    expect(linkAt(A, 0)).toEqual([]);
    expect(linkAt(A, 6)).toEqual([]);
    undo.undo();
    expect(A.text()).toBe('ab\n');
    expect(spansOf(A.doc).every((s) => !s.marks.has('link'))).toBe(true);
  });

  it('adds no second space when one follows; the caret steps over it', () => {
    const A = single();
    ensureTerminated(A.doc);
    insertText(A.doc, 0, 'a b');
    const r = insertChip(A.doc, 1, 'Ana', personUrl('Ana'));
    expect(A.text()).toBe('aAna b\n');
    expect(r.caret).toBe(5);
  });

  it('adds the space at a line end', () => {
    const A = single();
    ensureTerminated(A.doc);
    insertText(A.doc, 0, 'see');
    insertChip(A.doc, 3, 'Q3', meetingUrl('0123abcd'));
    expect(A.text()).toBe('seeQ3 \n');
  });

  it('turns line breaks and cell separators in the label into spaces; empty text is a no-op', () => {
    const A = single();
    ensureTerminated(A.doc);
    const r = insertChip(A.doc, 0, 'a\nb\u001fc', personUrl('x'));
    expect(A.text()).toBe('a b c \n');
    expect(r.inverse.length).toBeGreaterThan(0);
    const B = single();
    ensureTerminated(B.doc);
    expect(insertChip(B.doc, 0, '', personUrl('x'))).toEqual({ inverse: [], caret: 0 });
    expect(B.text()).toBe('\n');
  });
});
