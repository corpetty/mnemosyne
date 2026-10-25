// The guest page's live carets for the notes editor (phase 11, task 13): adapts GuestDoc to the
// editor's PresenceSource. Our selection goes out through `GuestDoc.setSel` (throttled there);
// everyone else's with a selection comes back as a caret, keyed by gid (the guests' own,
// "o:<key>" for people on the owner's side); the overlay colours them from `who`.
import type { CaretView, PresenceSource } from '$lib/notes/ui/types.js';
import type { GuestDoc, Person } from './doc';
import { isOwnerSide } from './display';

/**
 * Whose colour a person on this page has (carets.ts `caretColor` hashes it): a guest's gid; the
 * owner and people on the owner's side by NAME, so one person's two windows share a colour.
 */
export function whoOf(p: { gid: string; name: string }): string {
	if (p.gid === '' || isOwnerSide(p.gid)) return p.name || p.gid || 'host';
	return p.gid;
}

export function createGuestPresence(guest: GuestDoc): PresenceSource {
	const carets = (): CaretView[] =>
		(guest.others ?? [])
			.filter((p) => p.sel)
			.map((p) => ({
				key: p.gid,
				name: p.name || (isOwnerSide(p.gid) || p.gid === '' ? 'Someone' : 'Guest'),
				who: whoOf(p),
				sel: p.sel!
			}));
	const signature = () => JSON.stringify(carets());
	return {
		setSel: (sel) => guest.setSel(sel),
		carets,
		// GuestDoc notifies on every heartbeat; the editor hears only when a caret changed.
		subscribe(fn) {
			let last = signature();
			return guest.subscribe(() => {
				const now = signature();
				if (now === last) return;
				last = now;
				fn();
			});
		}
	};
}

/** One entry of the present list: the text as `peopleLine` writes it, and whose colour its dot has. */
export interface PersonPart {
	text: string;
	who: string;
}

/**
 * `peopleLine`'s entries one by one (their texts joined by ", " are exactly that line), so the
 * page can put each person's coloured dot before the name. Same order and same rules: the owner
 * first, then the owner's side (an entry named like the host adds nothing, the same label shows
 * once), then the guests.
 */
export function peopleParts(others: readonly Person[]): PersonPart[] {
	const host = others.find((p) => p.gid === '');
	const parts: PersonPart[] = [];
	if (host) parts.push({ text: `${host.name || 'Host'} (host)`, who: whoOf(host) });
	const seen = new Set<string>();
	for (const p of others) {
		if (!isOwnerSide(p.gid) || (host && p.name === host.name)) continue;
		const text = `${p.name || 'Someone'} (team)`;
		if (seen.has(text)) continue;
		seen.add(text);
		parts.push({ text, who: whoOf(p) });
	}
	for (const p of others) if (p.gid !== '' && !isOwnerSide(p.gid)) parts.push({ text: p.name || 'Guest', who: whoOf(p) });
	return parts;
}
