import { describe, expect, it } from 'vitest';
import { groupHistory } from './historyGroups.js';
import type { HistoryEvent } from '../types/index.js';

const ev = (kind: string, at: string, version?: number) =>
	({ kind, at, part: null, detail: version == null ? {} : { version } }) as unknown as HistoryEvent;

describe('groupHistory', () => {
	it('collapses consecutive automatic updates into one row', () => {
		const rows = groupHistory([
			ev('published', '2026-11-05T10:00:00Z', 1),
			ev('publication_auto_updated', '2026-11-05T10:05:00Z', 2),
			ev('publication_auto_updated', '2026-11-05T10:10:00Z', 3),
			ev('publication_auto_updated', '2026-11-05T10:15:00Z', 4),
			ev('summarized', '2026-11-05T10:20:00Z'),
			ev('publication_auto_updated', '2026-11-05T10:25:00Z', 5)
		]);
		expect(rows.map((r) => [r.event.kind, r.count])).toEqual([
			['published', 1],
			['publication_auto_updated', 3],
			['summarized', 1],
			['publication_auto_updated', 1]
		]);
		expect(rows[1].last.at).toBe('2026-11-05T10:15:00Z');
		expect(rows[3].last.detail).toEqual({ version: 5 });
	});
});
