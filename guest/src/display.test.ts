import { describe, expect, it } from 'vitest';
import { connectingLine, savedLine, formatHostTime, formatMeetingDate, ownersNotes, peopleLine, statusPill, typingLine } from './display';
import type { Person } from './doc';

const person = (gid: string, name: string, typing = false): Person => ({ gid, name, typing, at: 0 });

describe('formatMeetingDate', () => {
	it('reads a calendar date as that day', () => {
		expect(formatMeetingDate('2026-10-22', 'en-US')).toBe('October 22, 2026');
	});
	it('reads a timestamp', () => {
		expect(formatMeetingDate('2026-10-22T12:00:00', 'en-US')).toBe('October 22, 2026');
	});
	it('leaves unreadable text and handles empty', () => {
		expect(formatMeetingDate('soon', 'en-US')).toBe('soon');
		expect(formatMeetingDate('', 'en-US')).toBe('');
		expect(formatMeetingDate(null)).toBe('');
	});
});

describe('formatHostTime', () => {
	it('formats seconds and skips nothing', () => {
		expect(formatHostTime(1_793_000_000, 'en-US')).toMatch(/2026/);
		expect(formatHostTime(null)).toBe('');
		expect(formatHostTime(0)).toBe('');
	});
});

describe('lines', () => {
	it('owner', () => {
		expect(ownersNotes('Corey')).toBe("Corey's notes");
		expect(ownersNotes('')).toBe('');
	});
	it('people, the host marked', () => {
		expect(peopleLine([person('', 'Corey'), person('a', 'Bob')])).toBe('Corey (host), Bob');
		expect(peopleLine([])).toBe('');
	});
	it('typing', () => {
		expect(typingLine([])).toBe('');
		expect(typingLine(['Bob'])).toBe('Bob is typing…');
		expect(typingLine(['Ann', 'Bob'])).toBe('Ann and Bob are typing…');
		expect(typingLine(['Ann', 'Bob', 'Cy'])).toBe('Ann, Bob and Cy are typing…');
	});
	it('every status has a pill', () => {
		expect(statusPill('live')).toMatchObject({ label: 'Live', tone: 'green' });
		expect(statusPill('reconnecting').tone).toBe('amber');
		expect(statusPill('full').detail).toBe('Too many people are in these notes right now');
	});
});

describe('connectingLine', () => {
	it("names the owner's computer while iroh is tried, once the owner is known", () => {
		expect(connectingLine('connecting', 'iroh', 'Alice')).toBe("Connecting to Alice's computer…");
		expect(connectingLine('reconnecting', 'iroh', 'Alice')).toBe("Connecting to Alice's computer…");
		expect(connectingLine('connecting', 'iroh', '')).toBe("Connecting to the meeting host's computer…");
		expect(connectingLine('connecting', 'host', 'Alice')).toBe('Connecting…');
		expect(connectingLine('catching_up', 'iroh', 'Alice')).toBe('Connecting…');
		expect(connectingLine('connecting', null, null)).toBe('Connecting…');
	});
});

describe('savedLine', () => {
	it('says when it was saved and whose computer is off', () => {
		const at = Date.UTC(2026, 9, 25, 12, 0, 0);
		expect(savedLine(at, 'Alice', 'en-US')).toMatch(/^A saved copy from .*2026.*: Alice's computer is off\. It is read-only\.$/);
		expect(savedLine(at, '', 'en-US')).toContain("the meeting host's computer is off. It is read-only.");
		expect(savedLine(null, 'Alice')).toBe("A saved copy: Alice's computer is off. It is read-only.");
	});
	it('has a pill', () => {
		expect(statusPill('saved')).toMatchObject({ label: 'Saved copy', tone: 'gray' });
	});
});
