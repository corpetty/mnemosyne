import type { HistoryEvent } from '$lib/types/index.js';

/** One row of "What happened": a single event, or a run of automatic publication updates. */
export type HistoryRow = { event: HistoryEvent; count: number; last: HistoryEvent };

/**
 * Consecutive `publication_auto_updated` events collapse into one row (the newest event is
 * `last`, its `count` the number of versions); every other event is a row of its own.
 */
export function groupHistory(events: HistoryEvent[]): HistoryRow[] {
	const rows: HistoryRow[] = [];
	for (const event of events) {
		const prev = rows[rows.length - 1];
		if (event.kind === 'publication_auto_updated' && prev && prev.event.kind === 'publication_auto_updated') {
			prev.count++;
			prev.last = event;
		} else {
			rows.push({ event, count: 1, last: event });
		}
	}
	return rows;
}
