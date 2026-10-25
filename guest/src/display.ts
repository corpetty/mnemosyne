// Text the guest page shows: dates in the reader's own locale, the status pill, the presence line.
import type { GuestStatus, Person } from './doc';

/**
 * The meeting's date in the reader's locale. "2026-10-22" is a calendar date (no time zone, so
 * it is read as local midnight and never shifts a day); a full timestamp is shown in the reader's
 * time zone. Anything unreadable comes back as it was.
 */
export function formatMeetingDate(text: string | null | undefined, locale?: string): string {
	if (!text) return '';
	const plain = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
	const d = plain ? new Date(Number(plain[1]), Number(plain[2]) - 1, Number(plain[3])) : new Date(text);
	if (Number.isNaN(d.getTime())) return text;
	return d.toLocaleDateString(locale, { year: 'numeric', month: 'long', day: 'numeric' });
}

/** A host time (seconds since 1970) as a date and time in the reader's locale. */
export function formatHostTime(seconds: number | null | undefined, locale?: string): string {
	if (seconds == null || !Number.isFinite(seconds) || seconds <= 0) return '';
	return new Date(seconds * 1000).toLocaleString(locale, { dateStyle: 'medium', timeStyle: 'short' });
}

/** "Alice's notes"; "" when the owner is unknown. */
export function ownersNotes(owner: string | null | undefined): string {
	return owner ? `${owner}'s notes` : '';
}

export type PillTone = 'muted' | 'green' | 'amber' | 'gray' | 'red';

export interface Pill {
	label: string;
	tone: PillTone;
	/** A longer explanation (the pill's title and its screen-reader text). */
	detail: string;
}

export const FULL_MESSAGE = 'Too many people are in these notes right now';

export function statusPill(status: GuestStatus): Pill {
	switch (status) {
		case 'connecting':
			return { label: 'Connecting…', tone: 'muted', detail: '' };
		case 'catching_up':
			return { label: 'Catching up…', tone: 'muted', detail: '' };
		case 'live':
			return { label: 'Live', tone: 'green', detail: 'Edits reach everyone as you type' };
		case 'reconnecting':
			return { label: 'Reconnecting…', tone: 'amber', detail: 'Your edits are kept and sent once connected' };
		case 'ended':
			return { label: 'Ended', tone: 'gray', detail: 'These notes ended' };
		case 'gone':
			return { label: 'Gone', tone: 'red', detail: 'These notes are no longer shared' };
		case 'full':
			return { label: 'Full', tone: 'amber', detail: FULL_MESSAGE };
		case 'error':
			return { label: 'Error', tone: 'red', detail: "This link doesn't open these notes" };
	}
}

/** "Alice (host), Bob": the others in the room, the owner first and marked. */
export function peopleLine(others: readonly Person[]): string {
	return others.map((p) => (p.gid === '' ? `${p.name || 'Host'} (host)` : p.name || 'Guest')).join(', ');
}

/** "Bob is typing…", "Alice and Bob are typing…", "Alice, Bob and Carol are typing…"; "" for nobody. */
export function typingLine(names: readonly string[]): string {
	const ns = names.map((n) => n || 'Someone');
	if (!ns.length) return '';
	if (ns.length === 1) return `${ns[0]} is typing…`;
	return `${ns.slice(0, -1).join(', ')} and ${ns[ns.length - 1]} are typing…`;
}
