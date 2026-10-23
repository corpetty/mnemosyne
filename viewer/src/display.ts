// Text formatting for the viewer. English month names on purpose: the page is English, and a
// fixed form ("22 Oct 2026") reads the same everywhere and tests the same on every machine.

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function dayMonthYear(day: number, month: number, year: number): string {
	return `${day} ${MONTHS[month]} ${year}`;
}

/**
 * "2026-10-22" (a calendar date, no time zone) or a full ISO timestamp (shown in the reader's
 * own time zone) as "22 Oct 2026". Anything unreadable comes back as it was.
 */
export function formatDate(text: string | null | undefined): string {
	if (!text) return '';
	const plain = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
	if (plain) {
		const month = Number(plain[2]) - 1;
		if (month < 0 || month > 11) return text;
		return dayMonthYear(Number(plain[3]), month, Number(plain[1]));
	}
	const d = new Date(text);
	if (Number.isNaN(d.getTime())) return text;
	return dayMonthYear(d.getDate(), d.getMonth(), d.getFullYear());
}

/** A meeting's length: "38 min", "1 h", "1 h 5 min"; under half a minute is "1 min". */
export function formatDuration(seconds: number | null | undefined): string {
	if (seconds == null || !Number.isFinite(seconds) || seconds <= 0) return '';
	const minutes = Math.max(1, Math.round(seconds / 60));
	if (minutes < 60) return `${minutes} min`;
	const h = Math.floor(minutes / 60);
	const m = minutes % 60;
	return m ? `${h} h ${m} min` : `${h} h`;
}

/** A point in the meeting or a clip's length: "0:03", "5:00", "1:02:03". */
export function formatClock(seconds: number | null | undefined): string {
	const total = Math.max(0, Math.round(Number.isFinite(seconds) ? (seconds as number) : 0));
	const h = Math.floor(total / 3600);
	const m = Math.floor((total % 3600) / 60);
	const s = String(total % 60).padStart(2, '0');
	return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
}

/** The line under the title: "22 Oct 2026 · 38 min · Ana, Bo" (empty parts left out). */
export function metaLine(date: string, durationSeconds: number | null, attendees: string[]): string {
	return [formatDate(date), formatDuration(durationSeconds), attendees.join(', ')]
		.filter((p) => p)
		.join(' · ');
}

/** "Published with Mnemosyne · version 2 · expires 21 Jan 2027"; a preview says "Preview ·
 * version 2" (the version a publish would make). */
export function footerLine(version: number, expiresAt: string | null, preview = false): string {
	const parts = [preview ? 'Preview' : 'Published with Mnemosyne', `version ${version}`];
	if (expiresAt) parts.push(`expires ${formatDate(expiresAt)}`);
	return parts.join(' · ');
}
