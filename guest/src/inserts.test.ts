import { describe, expect, it } from 'vitest';
import { createGuestInserts } from './inserts';
import type { GuestDoc } from './doc';

const fake = (meta: unknown, others: { name: string }[] = []) =>
	({ meta, others }) as unknown as GuestDoc;

describe('guest inserts', () => {
	const t0 = Date.parse('2026-10-25T10:00:00Z');

	it('time follows the clock', () => {
		const g = fake({ clock: { part: 1, offset: 60, started_at: '2026-10-25T10:00:00Z' } });
		const ins = createGuestInserts(g, () => t0 + 65_000);
		expect(ins.time!()).toEqual({ part: 1, seconds: 125, label: '2:05' });
		expect(createGuestInserts(g, () => t0 - 5000).time!()?.seconds).toBe(60);
	});

	it('time is null without a clock', () => {
		expect(createGuestInserts(fake({})).time!()).toBeNull();
		expect(createGuestInserts(fake(null)).time!()).toBeNull();
	});

	it('people: prefix filter, guest suffix stripped, free text fallback', async () => {
		const ins = createGuestInserts(
			fake(null, [{ name: 'Ana (guest)' }, { name: 'Bob' }, { name: 'Ana (guest)' }, { name: '' }])
		);
		expect(await ins.people!('')).toEqual([{ name: 'Ana' }, { name: 'Bob' }]);
		expect(await ins.people!('b')).toEqual([{ name: 'Bob' }]);
		expect(await ins.people!('Zed')).toEqual([{ name: 'Zed' }]);
	});

	it('has no meetings and no chip click', () => {
		const ins = createGuestInserts(fake(null));
		expect(ins.meetings).toBeUndefined();
		expect(ins.onChipClick).toBeUndefined();
	});
});
