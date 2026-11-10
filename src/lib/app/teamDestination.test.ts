import { describe, expect, it } from 'vitest';
import {
  consentRequiredFor,
  defaultDestination,
  libraryFor,
  moveNeedsConsent,
  savePendingStart,
  takePendingStart,
  putInTeamReason
} from './teamDestination.js';

describe('the default destination', () => {
  it('is your library while you look at your library, whatever the work default', () => {
    expect(defaultDestination('mine', 'here')).toBe('mine');
    expect(defaultDestination('mine', 'host')).toBe('mine');
  });
  it('follows the work default in the team library', () => {
    expect(defaultDestination('team', 'here')).toBe('team-here');
    expect(defaultDestination('team', 'host')).toBe('team-host');
  });
});

describe('the library a recording is followed in', () => {
  it('own and for-the-team recordings are on this computer, streamed ones are the team\'s', () => {
    expect(libraryFor('mine')).toBe('mine');
    expect(libraryFor('team-here')).toBe('mine');
    expect(libraryFor('team-host')).toBe('team');
  });
});

describe('whose consent rule applies', () => {
  it('your library keeps the connected backend\'s rule', () => {
    expect(consentRequiredFor('mine', true, false)).toBe(true);
    expect(consentRequiredFor('mine', false, true)).toBe(false);
  });
  it('both team destinations follow the host\'s rule, not the connected backend\'s', () => {
    for (const d of ['team-here', 'team-host'] as const) {
      expect(consentRequiredFor(d, false, true)).toBe(true);
      expect(consentRequiredFor(d, true, false)).toBe(false);
    }
  });
});

describe('moves refused for consent', () => {
  it('is recognised by the host\'s message', () => {
    expect(moveNeedsConsent('409: The team asks how the people in the meeting agreed to be recorded')).toBe(true);
    expect(moveNeedsConsent('The team could not be reached')).toBe(false);
    expect(moveNeedsConsent('')).toBe(false);
  });
});

describe('meetings that cannot be put in the team', () => {
  it('own meetings can', () => expect(putInTeamReason({})).toBe(''));
  it('shared and foreign-origin ones cannot', () => {
    expect(putInTeamReason({ shared_from: 'c1' })).toMatch(/shared with you/);
    expect(putInTeamReason({ origin: 'peer1' })).toMatch(/another computer/);
    expect(putInTeamReason(null)).not.toBe('');
  });
});

describe('a recording started from the other library', () => {
  const mem = () => {
    const m = new Map<string, string>();
    return {
      getItem: (k: string) => m.get(k) ?? null,
      setItem: (k: string, v: string) => void m.set(k, v),
      removeItem: (k: string) => void m.delete(k)
    } as unknown as Storage;
  };
  it('is remembered once for the next page', () => {
    const s = mem();
    savePendingStart({ destination: 'team-here', fresh: true }, s, 1000);
    expect(takePendingStart(s, 2000)).toEqual({ destination: 'team-here', fresh: true });
    expect(takePendingStart(s, 2000)).toBeNull();
  });
  it('goes stale after a minute and ignores garbage', () => {
    const s = mem();
    savePendingStart({ destination: 'mine', fresh: false }, s, 1000);
    expect(takePendingStart(s, 1000 + 61_000)).toBeNull();
    s.setItem('mnemosyne.pendingRecord', '{"destination":"elsewhere","at":1}');
    expect(takePendingStart(s, 2)).toBeNull();
    s.setItem('mnemosyne.pendingRecord', 'not json');
    expect(takePendingStart(s, 2)).toBeNull();
  });
});
