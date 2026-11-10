import { describe, expect, it } from 'vitest';
import {
  consentRequiredFor,
  defaultDestination,
  libraryFor,
  moveNeedsConsent,
  savePendingStart,
  takePendingStart,
  putInTeamReason,
  teamLabel,
  isEmptyDraft,
  draftPlan,
  saveOpenAfterSwitch,
  takeOpenAfterSwitch
} from './teamDestination.js';

describe('the team name', () => {
  it('is Team on <host>, and "the team" before the host is known', () => {
    expect(teamLabel('Desk')).toBe('Team on Desk');
    expect(teamLabel('  Desk ')).toBe('Team on Desk');
    expect(teamLabel(null)).toBe('the team');
    expect(teamLabel('')).toBe('the team');
  });
});

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

describe('the draft a recording leaves behind', () => {
  const draft = { name: 'Untitled Session', status: 'created', transcript: [], notes: '', agenda: [], assets: [] };

  it('a new meeting is empty; anything done to it makes it content', () => {
    expect(isEmptyDraft(draft)).toBe(true);
    expect(isEmptyDraft({ name: 'Untitled Session' })).toBe(true);
    for (const touched of [
      { name: 'Standup' },
      { audio_file: '/x.wav' },
      { recordings: [{}] },
      { transcript: [{}] },
      { notes: 'a line' },
      { own_notes: 'a line' },
      { agenda: [{}] },
      { assets: [{}] },
      { external_notes: [{}] },
      { attendees: ['Ada'] },
      { bookmarks: [{}] },
      { summary: 'x' },
      { meeting_type: 'standup' },
      { status: 'completed' },
      { shared_from: 'someone' },
      { origin: 'peer' }
    ])
      expect(isEmptyDraft({ ...draft, ...touched }), JSON.stringify(touched)).toBe(false);
    expect(isEmptyDraft({ ...draft, notes: '  \n' })).toBe(true); // whitespace is nothing
    expect(isEmptyDraft(null)).toBe(false);
  });

  it('is recorded into when the recording stays in its library', () => {
    expect(draftPlan('mine', 'mine', draft)).toBe('use');
    expect(draftPlan('mine', 'team-here', draft)).toBe('use');
    expect(draftPlan('team', 'team-host', draft)).toBe('use');
  });

  it('is removed when the recording goes to the other library', () => {
    expect(draftPlan('team', 'team-here', draft)).toBe('remove');
    expect(draftPlan('team', 'mine', draft)).toBe('remove');
    expect(draftPlan('mine', 'team-host', draft)).toBe('remove');
  });

  it('is kept when it has content, wherever the recording goes', () => {
    const notes = { ...draft, notes: 'something' };
    for (const lib of ['mine', 'team'] as const)
      for (const d of ['mine', 'team-here', 'team-host'] as const) expect(draftPlan(lib, d, notes)).toBe('keep');
    expect(draftPlan('mine', 'mine', null)).toBe('keep');
  });
});

describe('opening a meeting after a library switch', () => {
  const mem = () => {
    const m = new Map<string, string>();
    return {
      getItem: (k: string) => m.get(k) ?? null,
      setItem: (k: string, v: string) => void m.set(k, v),
      removeItem: (k: string) => void m.delete(k)
    } as unknown as Storage;
  };
  it('is remembered once, and stale after a minute', () => {
    const s = mem();
    saveOpenAfterSwitch('host1', s, 1000);
    expect(takeOpenAfterSwitch(s, 2000)).toBe('host1');
    expect(takeOpenAfterSwitch(s, 2000)).toBeNull();
    saveOpenAfterSwitch('host1', s, 1000);
    expect(takeOpenAfterSwitch(s, 1000 + 61_000)).toBeNull();
  });
});
