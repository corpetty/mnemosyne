import { describe, expect, it } from 'vitest';
import { insertChip, personUrl } from '../chips.js';
import { deleteRange, insertTable, insertText, setLineKind, setLink, toggleInline } from '../commands.js';
import { CELL_SEP, ensureTerminated, lines, type Doc } from '../model.js';
import { pair, single } from '../test-harness.js';
import { filter, idPart, menuItems, placeMenu, queryOf, regionOf, triggerAt, triggerOf } from './slash.js';

function docOf(text: string): Doc {
  const A = single();
  ensureTerminated(A.doc);
  insertText(A.doc, 0, text);
  return A.doc;
}

const idAt = (doc: Doc, pos: number) => doc.web.seqIdAt(doc.obj, pos) as string;

describe('triggerAt', () => {
  it('opens on / and @ at a line start or after white space', () => {
    expect(triggerAt(docOf('/'), 1, '/')).toBe(true);
    expect(triggerAt(docOf('see /'), 5, '/')).toBe(true);
    expect(triggerAt(docOf('ab\n@'), 4, '@')).toBe(true);
    expect(triggerAt(docOf('call @'), 6, '@')).toBe(true);
    expect(triggerAt(docOf('a\t/'), 3, '/')).toBe(true);
    // A no-break space counts as a space (an engine may keep a trailing space as U+00A0).
    expect(triggerAt(docOf('x\u00a0@'), 3, '@')).toBe(true);
    expect(triggerAt(docOf('we decided,\u00a0/'), 13, '/')).toBe(true);
  });

  it('opens on the second [ of [[ only', () => {
    expect(triggerAt(docOf('[['), 2, '[')).toBe(true);
    expect(triggerAt(docOf('see [['), 6, '[')).toBe(true);
    expect(triggerAt(docOf('['), 1, '[')).toBe(false);
    expect(triggerAt(docOf('a['), 2, '[')).toBe(false);
    expect(triggerAt(docOf('x[['), 3, '[')).toBe(false);
  });

  it('does not open mid-word or for other characters', () => {
    expect(triggerAt(docOf('and/'), 4, '/')).toBe(false);
    expect(triggerAt(docOf('ana@'), 4, '@')).toBe(false);
    expect(triggerAt(docOf('http:/'), 6, '/')).toBe(false);
    expect(triggerAt(docOf('a #'), 3, '#')).toBe(false);
    // The character at the caret is not the trigger (stale caret).
    expect(triggerAt(docOf('a x'), 3, '/')).toBe(false);
  });

  it('does not open in a code line', () => {
    const doc = docOf('x /');
    setLineKind(doc, 0, 0, 'code');
    expect(triggerAt(doc, 3, '/')).toBe(false);
  });

  it('does not open inside inline code, typed in it or between two of its characters', () => {
    const typed = docOf('ab /cd');
    toggleInline(typed, 0, 6, 'code');
    expect(triggerAt(typed, 4, '/')).toBe(false);
    const between = docOf('ab cd');
    toggleInline(between, 0, 5, 'code');
    insertText(between, 3, '/');
    expect(triggerAt(between, 4, '/')).toBe(false);
    // Right after a code span (a closed mark does not grow) it opens.
    const after = docOf('ab /');
    toggleInline(after, 0, 2, 'code');
    expect(triggerAt(after, 4, '/')).toBe(true);
  });

  it('does not open inside a link or on a chip', () => {
    const doc = docOf('go to x');
    setLink(doc, 0, 7, 'https://example.com');
    insertText(doc, 6, '/');
    expect(triggerAt(doc, 7, '/')).toBe(false);
    const chip = docOf('');
    insertChip(chip, 0, '@Ana', personUrl('Ana'));
    expect(triggerAt(chip, 1, '@')).toBe(false);
  });

  it('does not open in a table', () => {
    const doc = docOf('');
    insertTable(doc, 0, 2, 2);
    insertText(doc, 0, '/');
    expect(lines(doc)[0].cells).not.toBeNull();
    expect(triggerAt(doc, 1, '/')).toBe(false);
    const cell = lines(doc)[1].cellStarts![1];
    insertText(doc, cell, '@');
    expect(triggerAt(doc, cell + 1, '@')).toBe(false);
  });

  it('knows its triggers', () => {
    expect([triggerOf('/'), triggerOf('@'), triggerOf('['), triggerOf('x')]).toEqual(['/', '@', '[[', null]);
  });
});

describe('queryOf', () => {
  it('is the text between the trigger and the caret', () => {
    const doc = docOf('a /hea');
    const id = idAt(doc, 2);
    expect(queryOf(doc, id, 3)).toBe('');
    expect(queryOf(doc, id, 6)).toBe('hea');
    const at = docOf('[[plan');
    expect(queryOf(at, idAt(at, 0), 6, '[[')).toBe('plan');
  });

  it('follows the trigger through a remote insert before it', () => {
    const { relay, A, B } = pair();
    ensureTerminated(A.doc);
    insertText(A.doc, 0, 'x /hea');
    relay.settle();
    const id = idAt(A.doc, 2);
    insertText(B.doc, 0, 'Hello ');
    relay.settle();
    expect(A.text()).toBe('Hello x /hea\n');
    expect(queryOf(A.doc, id, 12)).toBe('hea');
    // The old offsets now point elsewhere: the caret before the trigger has no query.
    expect(queryOf(A.doc, id, 6)).toBeNull();
  });

  it('is null when the trigger is gone or changed', () => {
    const doc = docOf('a /hea');
    const id = idAt(doc, 2);
    deleteRange(doc, 2, 3);
    expect(queryOf(doc, id, 5)).toBeNull();
    const at = docOf('[[x');
    const first = idAt(at, 0);
    deleteRange(at, 1, 2); // "[x": no longer [[
    expect(queryOf(at, first, 2, '[[')).toBeNull();
  });

  it('is null when the caret moved before the query or off its line', () => {
    const doc = docOf('a /hea\nnext');
    const id = idAt(doc, 2);
    expect(queryOf(doc, id, 2)).toBeNull();
    expect(queryOf(doc, id, 1)).toBeNull();
    expect(queryOf(doc, id, 9)).toBeNull();
  });

  it('is null past the length limit or across a cell', () => {
    const doc = docOf('/' + 'x'.repeat(41));
    const id = idAt(doc, 0);
    expect(queryOf(doc, id, 41)).toBe('x'.repeat(40));
    expect(queryOf(doc, id, 42)).toBeNull();
    const cell = docOf(`/a${CELL_SEP}b`);
    expect(queryOf(cell, idAt(cell, 0), 4)).toBeNull();
  });

  it('counts code points', () => {
    const doc = docOf('😀 /a😀b');
    const id = idAt(doc, 2);
    expect(queryOf(doc, id, 6)).toBe('a😀b');
  });
});

describe('regionOf', () => {
  it('covers the trigger and the query', () => {
    const doc = docOf('a /hea');
    expect(regionOf(doc, { anchor: { id: idAt(doc, 2), trigger: '/' }, cut: null }, 6)).toEqual({ from: 2, to: 6, start: 3, query: 'hea' });
  });

  it('without a trigger: the query follows the anchor element (null = the document start)', () => {
    const doc = docOf('ab cd');
    expect(regionOf(doc, { anchor: { id: idAt(doc, 2), trigger: '' }, cut: null }, 5)).toEqual({ from: 3, to: 5, start: 3, query: 'cd' });
    expect(regionOf(doc, { anchor: { id: null, trigger: '' }, cut: null }, 2)).toEqual({ from: 0, to: 2, start: 0, query: 'ab' });
  });

  it('a cut reaches back to the trigger a picker came from', () => {
    const doc = docOf('x /personana');
    const span = { anchor: { id: idAt(doc, 8), trigger: '' as const }, cut: idAt(doc, 2) };
    expect(regionOf(doc, span, 12)).toEqual({ from: 2, to: 12, start: 9, query: 'ana' });
    deleteRange(doc, 2, 3);
    expect(regionOf(doc, span, 11)).toBeNull();
  });
});

describe('filter', () => {
  const items = menuItems({ time: '12:04', people: true, meetings: true, line: 'text' });
  const names = (q: string) => filter(items, q).map((i) => i.name);

  it('keeps everything for an empty query, in order', () => {
    expect(names('')).toEqual(items.map((i) => i.name));
  });

  it('puts prefix matches of names and aliases first, then substrings, ignoring case', () => {
    expect(names('HEA')).toEqual(['Heading 1', 'Heading 2', 'Heading 3']);
    expect(names('h1')).toEqual(['Heading 1']);
    expect(names('title')).toEqual(['Heading 1', 'Heading 2']); // "subtitle" contains it
    expect(names('todo')).toEqual(['Task list']);
    expect(names('list')[0]).toBe('Bulleted list');
    expect(names('list')).toContain('Numbered list');
    expect(names('now')).toEqual(['Time now']);
    expect(names('mention')).toEqual(['Person']);
    expect(names('meeting')).toEqual(['Earlier meeting']);
    expect(names('zzz')).toEqual([]);
    const ord = filter([{ name: 'Bob Ana' }, { name: 'Ana' }, { name: 'Banana' }], 'ana').map((p) => p.name);
    expect(ord).toEqual(['Ana', 'Bob Ana', 'Banana']);
  });
});

describe('menuItems', () => {
  it('shows This meeting before Blocks, each insert only when its provider answers', () => {
    const all = menuItems({ time: '3:05', people: true, meetings: true, line: 'text' });
    expect(all.map((i) => i.name)).toEqual([
      'Time now',
      'Action item',
      'Person',
      'Earlier meeting',
      'Text',
      'Heading 1',
      'Heading 2',
      'Heading 3',
      'Bulleted list',
      'Numbered list',
      'Task list',
      'Quote',
      'Code block',
      'Table'
    ]);
    expect(all[0].hint).toBe('3:05');
    const bare = menuItems({ time: null, people: false, meetings: false, line: 'text' });
    expect(bare.filter((i) => i.group === 'meeting').map((i) => i.name)).toEqual(['Action item']);
    expect(new Set(all.map((i) => i.key)).size).toBe(all.length);
  });

  it('leaves chips out of a code line and line kinds out of a table row', () => {
    expect(menuItems({ time: '1:00', people: true, meetings: true, line: 'code' }).every((i) => i.group === 'blocks')).toBe(true);
    expect(menuItems({ time: '1:00', people: true, meetings: true, line: 'table' }).map((i) => i.name)).toEqual(['Time now', 'Person', 'Earlier meeting']);
  });

  it('makes DOM-safe id parts', () => {
    expect(idPart('Ana María 😀')).toMatch(/^[A-Za-z0-9_-]+$/);
    expect(idPart('a b')).not.toBe(idPart('a_b'));
  });
});

describe('placeMenu', () => {
  const box = { left: 100, top: 100, right: 500, bottom: 900 };
  const menu = { width: 200, height: 150 };
  it('goes below the caret, left aligned with it, in the box coordinates', () => {
    const p = placeMenu({ caret: { left: 150, top: 200, right: 150, bottom: 218 }, box, menu, limitBottom: 1000 });
    expect(p).toEqual({ top: 122, left: 50, side: 'below' });
  });

  it('flips above near the bottom, adds the scroll and stays in the box', () => {
    const p = placeMenu({ caret: { left: 480, top: 800, right: 480, bottom: 818 }, box, menu, limitBottom: 900, scrollTop: 40, scrollLeft: 0 });
    expect(p).toEqual({ top: 800 - 4 - 150 - 100 + 40, left: 200, side: 'above' });
  });

  it('stays below when there is no room above either', () => {
    const p = placeMenu({ caret: { left: 100, top: 110, right: 100, bottom: 128 }, box, menu, limitBottom: 200, limitTop: 100 });
    expect(p.side).toBe('below');
  });
});
