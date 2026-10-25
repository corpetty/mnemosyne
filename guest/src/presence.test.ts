import { describe, expect, it } from 'vitest';
import { caretColor } from '$lib/notes/ui/carets.js';
import { peopleLine } from './display';
import type { Person } from './doc';
import { peopleParts, whoOf } from './presence';

const person = (gid: string, name: string): Person => ({ gid, name, typing: false, at: 0 });

describe('whoOf', () => {
  it('is the gid for a guest and the name for the owner and the owner side', () => {
    expect(whoOf(person('g-77', 'Ben'))).toBe('g-77');
    expect(whoOf(person('', 'Corey'))).toBe('Corey');
    expect(whoOf(person('o:k1', 'Corey'))).toBe('Corey');
    expect(whoOf(person('o:k1', ''))).toBe('o:k1');
    expect(whoOf(person('', ''))).toBe('host');
  });
  it('gives one person two windows one colour', () => {
    expect(caretColor(whoOf(person('', 'Corey')))).toBe(caretColor(whoOf(person('o:k9', 'Corey'))));
  });
});

describe('peopleParts', () => {
  const rooms: Person[][] = [
    [],
    [person('', 'Corey'), person('g1', 'Ben'), person('o:k1', 'Ana'), person('o:k2', 'Ana'), person('o:k3', 'Corey')],
    [person('g1', ''), person('o:k1', '')],
    [person('', ''), person('g2', 'Zed')]
  ];
  it('are the entries of peopleLine, so the text of the list does not change', () => {
    for (const others of rooms) expect(peopleParts(others).map((p) => p.text).join(', ')).toBe(peopleLine(others));
  });
  it('colour each entry by who it is', () => {
    expect(peopleParts(rooms[1]).map((p) => p.who)).toEqual(['Corey', 'Ana', 'g1']);
  });
});
