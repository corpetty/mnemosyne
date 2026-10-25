// What the guest page's notes editor can insert (phase 11, task 20): the time from META `clock`
// and the people in the room. No meetings, no chip clicks (chips are plain on this page).
import type { InsertProvider } from '$lib/notes/ui/types.js';
import { formatClock } from '$lib/notes/chips.js';
import type { GuestDoc } from './doc';

const stripGuest = (name: string): string => name.replace(/\s*\(guest\)\s*$/i, '').trim();

export function createGuestInserts(guest: GuestDoc, now: () => number = Date.now): InsertProvider {
	return {
		time() {
			const c = guest.meta?.clock;
			if (!c) return null;
			const started = Date.parse(c.started_at);
			if (Number.isNaN(started)) return null;
			const seconds = Math.max(0, (now() - started) / 1000);
			return { part: c.part, seconds: c.offset + seconds, label: formatClock(c.offset + seconds) };
		},
		async people(q) {
			const names: string[] = [];
			for (const p of guest.others) {
				const n = stripGuest(p.name);
				if (n && !names.includes(n)) names.push(n);
			}
			const want = q.trim().toLowerCase();
			const hits = names.filter((n) => n.toLowerCase().startsWith(want));
			if (hits.length === 0 && want) return [{ name: q.trim() }];
			return hits.map((name) => ({ name }));
		}
	};
}
