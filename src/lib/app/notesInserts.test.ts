import { describe, expect, it } from 'vitest';
import { locate, meetingLabel, partOffsets, pickMeetings, pickPeople, recordingTime, seekTarget } from './notesInserts.js';

const rec = (part: number, offset: number) => ({ part, offset });
// Two sources per part share the offset: part 0 at 0, part 1 at 600.5, part 2 at 1500.
const recordings = [rec(0, 0), rec(0, 0), rec(1, 600.5), rec(1, 600.5), rec(2, 1500)];

describe('recordingTime', () => {
  const clock = { part: 1, offset: 600.5, started_at: '2026-10-25T10:00:00.000+00:00' };
  const at = (s: number) => Date.parse('2026-10-25T10:00:00.000Z') + s * 1000;

  it('counts whole seconds into the part and labels the joined timeline', () => {
    expect(recordingTime(clock, at(75.9))).toEqual({ part: 1, seconds: 75, label: '11:16' });
  });

  it('is never negative when the clocks disagree', () => {
    expect(recordingTime(clock, at(-5))).toEqual({ part: 1, seconds: 0, label: '10:00' });
  });

  it('is null for an unreadable start', () => {
    expect(recordingTime({ ...clock, started_at: 'nope' }, at(1))).toBeNull();
  });
});

describe('locate', () => {
  it('puts a position in the last part that has started', () => {
    expect(locate(0, recordings)).toEqual({ part: 0, seconds: 0, label: '0:00' });
    expect(locate(599.9, recordings)).toEqual({ part: 0, seconds: 599, label: '9:59' });
    expect(locate(600.5, recordings)).toEqual({ part: 1, seconds: 0, label: '10:00' });
    expect(locate(1499, recordings)).toEqual({ part: 1, seconds: 898, label: '24:59' });
    expect(locate(3700, recordings)).toEqual({ part: 2, seconds: 2200, label: '1:01:40' });
  });

  it('treats a meeting without recordings as one part from 0', () => {
    expect(locate(42, [])).toEqual({ part: 0, seconds: 42, label: '0:42' });
  });

  it('refuses a bad position and clamps a negative one', () => {
    expect(locate(Number.NaN, recordings)).toBeNull();
    expect(locate(-3, recordings)).toEqual({ part: 0, seconds: 0, label: '0:00' });
  });

  it('round-trips with seekTarget to within a second', () => {
    const t = locate(1234.7, recordings)!;
    const back = seekTarget(t, recordings)!;
    expect(back).toBeLessThanOrEqual(1234.7);
    expect(1234.7 - back).toBeLessThan(1);
  });
});

describe('partOffsets / seekTarget', () => {
  it('lists each part once', () => {
    expect(partOffsets(recordings)).toEqual([
      { part: 0, offset: 0 },
      { part: 1, offset: 600.5 },
      { part: 2, offset: 1500 }
    ]);
  });

  it('adds the part offset, and knows no part that is not there', () => {
    expect(seekTarget({ part: 1, seconds: 30 }, recordings)).toBe(630.5);
    expect(seekTarget({ part: 5, seconds: 30 }, recordings)).toBeNull();
    expect(seekTarget({ part: 0, seconds: 30 }, [])).toBe(30);
    expect(seekTarget({ part: 1, seconds: 30 }, [])).toBeNull();
  });
});

describe('pickPeople', () => {
  const others = [
    { name: 'ada lovelace', meetings: 3, last_seen: null, open_tasks: 0, has_voice: false },
    { name: 'Alan Turing', meetings: 1, last_seen: null, open_tasks: 0, has_voice: false },
    { name: 'Grace Hopper', meetings: 2, last_seen: null, open_tasks: 0, has_voice: false }
  ];

  it('lists this meeting first and each person once, whatever the case', () => {
    const got = pickPeople('', ['Ada Lovelace', 'Bo'], others);
    expect(got.map((p) => p.name)).toEqual(['Ada Lovelace', 'Bo', 'Alan Turing', 'Grace Hopper']);
    expect(got[0].hint).toBe('In this meeting');
    expect(got[2].hint).toBe('1 meeting');
  });

  it('puts prefix matches before word and inside matches', () => {
    const got = pickPeople('ho', ['Rhonda'], others);
    expect(got.map((p) => p.name)).toEqual(['Grace Hopper', 'Rhonda']);
    expect(pickPeople('al', [], others).map((p) => p.name)).toEqual(['Alan Turing']);
    expect(pickPeople('zzz', ['Bo'], others)).toEqual([]);
  });

  it('stops at eight', () => {
    const many = Array.from({ length: 20 }, (_, i) => `Person ${i}`);
    expect(pickPeople('', many, [])).toHaveLength(8);
  });
});

describe('pickMeetings', () => {
  const s = (id: string, name: string, created_at: string) => ({ id, name, created_at });
  const list = [
    s('a', 'Standup', '2026-10-20T09:00:00'),
    s('b', 'Planning', '2026-10-24T09:00:00'),
    s('c', 'Planning review', '2026-10-22T09:00:00'),
    s('d', '', '2026-10-23T09:00:00')
  ];

  it('is newest first and leaves this meeting out', () => {
    expect(pickMeetings('', list, 'b').map((m) => m.id)).toEqual(['d', 'c', 'a']);
  });

  it('filters by title', () => {
    expect(pickMeetings('plan', list, 'x').map((m) => m.id)).toEqual(['b', 'c']);
  });

  it('labels with the title and the date', () => {
    expect(meetingLabel(list[0])).toMatch(/^Standup · .+/);
    expect(meetingLabel(list[3])).toMatch(/^Untitled · /);
  });

  it('stops at eight', () => {
    const many = Array.from({ length: 20 }, (_, i) => s(`m${i}`, `M${i}`, `2026-10-${String(i + 1).padStart(2, '0')}T09:00:00`));
    expect(pickMeetings('', many, 'x')).toHaveLength(8);
  });
});
