import { describe, expect, it } from 'vitest';
import { formatHostTime, formatMeetingDate, ownersNotes, peopleLine, statusPill, typingLine } from './display';
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
