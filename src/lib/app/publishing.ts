// Pure helpers for the Publish dialog (components/PublishDialog.svelte, stores/publishing.svelte.ts).
// The rules mirror backend services/publishing.py where they overlap; the backend has the last word.

import type { QuoteSpan, SessionDetail } from '$lib/types/index.js';

/** Where a publication goes: served by this computer, a publish host, or one HTML file. */
export type Target = 'local' | 'host' | 'file';

/** The meeting's people (as backend `meeting_people`): calendar attendees, then speakers that
 * have a name; each once. Names already chosen for a publication come first, so a saved
 * selection keeps its order. */
const GENERIC_SPEAKER = /^(SPEAKER_\d+|UNKNOWN|Speaker \d+)$/i;

export function meetingPeople(
	session: Pick<SessionDetail, 'attendees' | 'participants'> | null | undefined,
	chosen: string[] = []
): string[] {
	const out: string[] = [];
	const seen = new Set<string>();
	for (const raw of [...chosen, ...(session?.attendees ?? []), ...(session?.participants ?? [])]) {
		const n = (raw ?? '').split(/\s+/).filter(Boolean).join(' ');
		if (!n || GENERIC_SPEAKER.test(n) || seen.has(n.toLowerCase())) continue;
		seen.add(n.toLowerCase());
		out.push(n);
	}
	return out;
}

/** The same stretch of the meeting (backend `_quote_key`, to the millisecond). */
export function quoteKey(q: Pick<QuoteSpan, 'part' | 'start' | 'end'>): string {
	return `${q.part ?? ''}|${q.start.toFixed(3)}|${q.end.toFixed(3)}`;
}

/** `existing` followed by those of `added` it does not have yet. */
export function mergeQuotes(existing: QuoteSpan[], added: QuoteSpan[]): QuoteSpan[] {
	const keys = new Set(existing.map(quoteKey));
	const out = [...existing];
	for (const q of added) {
		const k = quoteKey(q);
		if (keys.has(k)) continue;
		keys.add(k);
		out.push(q);
	}
	return out;
}

/** A span on the joined timeline: `part` spans are shifted by that part's offset. */
export function joinedSpan(
	session: Pick<SessionDetail, 'recordings'> | null | undefined,
	q: Pick<QuoteSpan, 'part' | 'start' | 'end'>
): { start: number; end: number } {
	let off = 0;
	if (q.part != null) {
		const offs = (session?.recordings ?? []).filter((r) => r.part === q.part).map((r) => r.offset);
		off = offs.length ? Math.min(...offs) : 0;
	}
	const a = q.start + off;
	const b = q.end + off;
	return a <= b ? { start: a, end: b } : { start: b, end: a };
}

/** The transcript lines a quote takes (backend `_lines`): those whose middle is inside it. */
function quoteLines(
	session: Pick<SessionDetail, 'recordings' | 'transcript'> | null | undefined,
	q: Pick<QuoteSpan, 'part' | 'start' | 'end'>
) {
	const { start, end } = joinedSpan(session, q);
	return (session?.transcript ?? []).filter((s) => {
		const mid = (s.start + s.end) / 2;
		return start - 0.01 <= mid && mid <= end + 0.01;
	});
}

/** A quote in the preview manifest; its clip points at this backend's audio. */
export interface PreviewQuote {
	id: string;
	text: string;
	speaker: string | null;
	start: number;
	end: number;
	clip: { preview_url: string; seconds: number; mime: string } | null;
}

/** The preview's quote for a chosen span (matched by its first line's start and last end,
 * as the backend rounds them), or null when it has none (no lines inside, or a duplicate). */
export function previewQuoteFor(
	session: Pick<SessionDetail, 'recordings' | 'transcript'> | null | undefined,
	q: Pick<QuoteSpan, 'part' | 'start' | 'end'>,
	quotes: PreviewQuote[] | null | undefined
): PreviewQuote | null {
	const lines = quoteLines(session, q);
	if (!lines.length || !quotes?.length) return null;
	const start = Math.round(lines[0].start * 100) / 100;
	const end = Math.round(Math.max(...lines.map((l) => l.end)) * 100) / 100;
	return quotes.find((p) => Math.abs(p.start - start) < 0.006 && Math.abs(p.end - end) < 0.006) ?? null;
}

/** The transcript text a quote would show: the lines whose middle is inside it, shortened. */
export function quoteExcerpt(
	session: Pick<SessionDetail, 'recordings' | 'transcript'> | null | undefined,
	q: Pick<QuoteSpan, 'part' | 'start' | 'end'>,
	max = 160
): string {
	const text = quoteLines(session, q)
		.map((s) => s.text.trim())
		.filter(Boolean)
		.join(' ');
	return text.length > max ? `${text.slice(0, max - 1).trimEnd()}…` : text;
}

/** "1:05" for a point in the meeting. */
export function clock(seconds: number): string {
	const t = Math.max(0, Math.round(seconds));
	const h = Math.floor(t / 3600);
	const m = Math.floor((t % 3600) / 60);
	const s = String(t % 60).padStart(2, '0');
	return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
}

export const EXPIRY_DAYS = [30, 90, 365] as const;

/** The expiry choices for a target: never only for a file; a custom saved value is kept. */
export function expiryChoices(target: Target, current: number | null): (number | null)[] {
	const out: (number | null)[] = [...EXPIRY_DAYS];
	if (current != null && !out.includes(current) && current <= 365) out.push(current);
	out.sort((a, b) => (a as number) - (b as number));
	if (target === 'file') out.push(null);
	return out;
}

/** An expiry that fits the target: never is a file's only; at most 365 days otherwise. */
export function fitExpiry(target: Target, days: number | null, fallback: number): number | null {
	if (target === 'file') return days;
	if (days == null) return Math.min(365, Math.max(1, fallback));
	return Math.min(365, Math.max(1, days));
}

export function expiryLabel(days: number | null): string {
	if (days == null) return 'Never';
	if (days === 365) return '1 year';
	return `${days} days`;
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** "3 Oct", or "3 Oct 2027" outside the current year (`now`'s). Fixed English like the viewer. */
export function shortDate(iso: string | null | undefined, now = new Date()): string {
	if (!iso) return '';
	const d = new Date(iso);
	if (Number.isNaN(d.getTime())) return '';
	const day = `${d.getDate()} ${MONTHS[d.getMonth()]}`;
	return d.getFullYear() === now.getFullYear() ? day : `${day} ${d.getFullYear()}`;
}

/** Where a publication is, for lists and history: "this computer" or the host's name. */
export function whereLabel(where: string, hostUrl = ''): string {
	if (where === 'host') {
		try {
			return hostUrl ? new URL(hostUrl).host : 'the publish host';
		} catch {
			return hostUrl || 'the publish host';
		}
	}
	return 'this computer';
}

/** Passwords for a file: none, or at least 8 characters (backend publication_file). */
export function passwordProblem(password: string): string {
	return password && password.length < 8 ? 'At least 8 characters' : '';
}
