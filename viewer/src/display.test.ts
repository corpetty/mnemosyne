import { describe, expect, it } from 'vitest';
import { footerLine, formatClock, formatDate, formatDuration, metaLine } from './display';

describe('dates', () => {
	it('formats a calendar date without shifting it', () => {
		expect(formatDate('2026-10-22')).toBe('22 Oct 2026');
		expect(formatDate('2027-01-01')).toBe('1 Jan 2027');
	});
	it('formats a timestamp in local time', () => {
		const iso = '2027-01-21T12:00:00Z';
		const d = new Date(iso);
		const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
		expect(formatDate(iso)).toBe(`${d.getDate()} ${months[d.getMonth()]} ${d.getFullYear()}`);
	});
	it('passes unreadable text through', () => {
		expect(formatDate('')).toBe('');
		expect(formatDate(null)).toBe('');
		expect(formatDate('soon')).toBe('soon');
		expect(formatDate('2026-13-01')).toBe('2026-13-01');
	});
});

describe('durations and clocks', () => {
	it('rounds meetings to minutes', () => {
		expect(formatDuration(1830.5)).toBe('31 min');
		expect(formatDuration(38 * 60)).toBe('38 min');
		expect(formatDuration(10)).toBe('1 min');
		expect(formatDuration(3600)).toBe('1 h');
		expect(formatDuration(3900)).toBe('1 h 5 min');
		expect(formatDuration(null)).toBe('');
		expect(formatDuration(0)).toBe('');
	});
	it('shows points and clip lengths as a clock', () => {
		expect(formatClock(0)).toBe('0:00');
		expect(formatClock(2.75)).toBe('0:03');
		expect(formatClock(300.5)).toBe('5:01');
		expect(formatClock(3723)).toBe('1:02:03');
		expect(formatClock(Number.NaN)).toBe('0:00');
	});
});

describe('lines', () => {
	it('joins the meta line and leaves out empty parts', () => {
		expect(metaLine('2026-10-22', 38 * 60, ['Ana', 'Bo'])).toBe('22 Oct 2026 · 38 min · Ana, Bo');
		expect(metaLine('2026-10-22', null, [])).toBe('22 Oct 2026');
	});
	it('writes the footer', () => {
		expect(footerLine(2, '2027-01-21')).toBe('Published with Mnemosyne · version 2 · expires 21 Jan 2027');
		expect(footerLine(1, null)).toBe('Published with Mnemosyne · version 1');
		expect(footerLine(4, null, true)).toBe('Preview · version 4');
	});
});
