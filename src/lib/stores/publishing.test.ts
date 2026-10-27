import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { backendMediaUrl } from '../api/backend.js';
import {
	expiryChoices,
	fitExpiry,
	meetingPeople,
	mergeQuotes,
	previewQuoteFor,
	quoteExcerpt,
	shortDate
} from '../app/publishing.js';
import { connectionState } from './connection.svelte.js';
import { errorText, PublishingState, type PublishingApi } from './publishing.svelte.js';
import type { Job, Publication, PublicationDefaults, PublicationSelection, QuoteSpan } from '../types/index.js';

const span = (start: number, end: number, with_clip = false): QuoteSpan => ({ start, end, part: null, with_clip });

const session = {
	id: 's1',
	name: 'Waku sync',
	attendees: ['Ada Lovelace', 'Bo'],
	participants: ['SPEAKER_00', 'bo', 'Grace Hopper'],
	recordings: [
		{ part: 0, offset: 0 },
		{ part: 1, offset: 100 }
	],
	transcript: [
		{ start: 0, end: 4, text: 'The Waku migration is code complete.', speaker: 'Ada Lovelace', words: null },
		{ start: 4, end: 8, text: 'Great, ship it.', speaker: 'Bo', words: null },
		{ start: 100, end: 104, text: 'Second part line.', speaker: 'Bo', words: null }
	]
} as never;

function defaults(extra: Partial<PublicationDefaults> = {}): PublicationDefaults {
	return {
		title: '',
		summary: true,
		decisions: true,
		questions: true,
		action_items: true,
		chapters: true,
		topics: true,
		notes: false,
		attendees: ['Ada Lovelace', 'Bo', 'Grace Hopper'],
		names: 'as_is',
		hide_identifiers: true,
		quotes: [],
		where: 'local',
		expires_days: 90,
		local_reach: 'this_device',
		local_url: '',
		host_url: '',
		host_is_ours: false,
		...extra
	};
}

function job(status: Job['status'], result: Job['result'] = null, message = ''): Job {
	return { id: 'j1', kind: 'publish', status, message, result, error: status === 'failed' ? 'boom' : null } as Job;
}

function fakeApi(over: Partial<PublishingApi> = {}) {
	const base = {
		getSettings: vi.fn(async () => ({
			values: { allow_publishing: false, publish_host: '', publish_default_expiry_days: 60 }
		})),
		getPublication: vi.fn(async () => null),
		getPublicationDefaults: vi.fn(async () => defaults()),
		previewPublication: vi.fn(async (_id: string, sel: PublicationSelection): Promise<unknown> => ({ title: sel.title, quotes: [] })),
		publishSession: vi.fn(async () => job('running', null, 'Encrypting')),
		unpublishSession: vi.fn(async () => ({}) as Publication),
		listPublications: vi.fn(async () => []),
		startPublicationFile: vi.fn(async (_id: string, _sel: PublicationSelection, _password: string | null) => job('running')),
		getJob: vi.fn(async () => job('completed', { publication: { id: 'p1', version: 1 } })),
		saveFile: vi.fn(async () => {})
	};
	return { ...base, ...over } as typeof base;
}

describe('the Publish dialog store', () => {
	beforeEach(() => vi.useFakeTimers());
	afterEach(() => vi.useRealTimers());

	it('opens with the defaults: the meeting name as title, pending quotes added, people in order', async () => {
		const api = fakeApi();
		const st = new PublishingState(api, 400, 10);
		expect(st.addPending('s1', span(0, 4))).toBe(1);
		expect(st.addPending('s1', span(0, 4))).toBe(1); // the same stretch once
		expect(st.addPending('s1', span(4, 8))).toBe(2);
		await st.open(session);
		expect(st.selection?.title).toBe('Waku sync');
		expect(st.selection?.quotes.map((q) => q.start)).toEqual([0, 4]);
		expect(st.pending.s1).toBeUndefined();
		expect(st.people).toEqual(['Ada Lovelace', 'Bo', 'Grace Hopper']);
		expect(st.defaultExpiry).toBe(60);
		expect(api.previewPublication).toHaveBeenCalledTimes(1); // the first preview is not delayed
	});

	it('debounces the preview: one request with the latest choices', async () => {
		const api = fakeApi();
		const st = new PublishingState(api, 400, 10);
		await st.open(session);
		api.previewPublication.mockClear();
		st.set({ decisions: false });
		await vi.advanceTimersByTimeAsync(200);
		st.set({ title: 'Shared' });
		await vi.advanceTimersByTimeAsync(399);
		expect(api.previewPublication).not.toHaveBeenCalled();
		await vi.advanceTimersByTimeAsync(1);
		expect(api.previewPublication).toHaveBeenCalledTimes(1);
		const sent = api.previewPublication.mock.calls[0][1];
		expect(sent.decisions).toBe(false);
		expect(sent.title).toBe('Shared');
	});

	it('keeps the newest preview when an older answer arrives late', async () => {
		let release: (v: unknown) => void = () => {};
		const api = fakeApi();
		const st = new PublishingState(api, 400, 10);
		await st.open(session);
		api.previewPublication.mockImplementationOnce(() => new Promise((r) => (release = r)));
		const slow = st.refreshPreview();
		await st.refreshPreview(); // answers at once: { title: 'Waku sync' }
		release({ title: 'stale' });
		await slow;
		expect((st.preview as { title: string }).title).toBe('Waku sync');
	});

	it('fits the expiry to where it goes: never only for a file', async () => {
		const st = new PublishingState(fakeApi(), 400, 10);
		await st.open(session);
		st.setTarget('file');
		st.set({ expires_days: null });
		expect(st.toSend()?.expires_days).toBeNull();
		expect(st.toSend()?.where).toBe('local');
		st.setTarget('local');
		expect(st.selection?.expires_days).toBe(60); // the default, as never is a file's only
		st.set({ expires_days: 900 });
		expect(st.toSend()?.expires_days).toBe(365);
	});

	it('ticks people in the meeting order and edits quotes by their span', async () => {
		const st = new PublishingState(fakeApi(), 400, 10);
		await st.open(session);
		st.toggleAttendee('Ada Lovelace');
		expect(st.selection?.attendees).toEqual(['Bo', 'Grace Hopper']);
		st.toggleAttendee('Ada Lovelace');
		expect(st.selection?.attendees).toEqual(['Ada Lovelace', 'Bo', 'Grace Hopper']);
		st.setQuotes([span(0, 4), span(4, 8)]);
		st.setClip(span(4, 8), true);
		expect(st.selection?.quotes.map((q) => q.with_clip)).toEqual([false, true]);
		st.removeQuote(span(0, 4, true)); // with_clip is not part of the identity
		expect(st.selection?.quotes.map((q) => q.start)).toEqual([4]);
		// With the dialog open, quotes from the transcript go straight into it.
		expect(st.addPending('s1', span(100, 104))).toBe(2);
		expect(st.pending.s1).toBeUndefined();
	});

	it('follows the publish job and keeps the publication', async () => {
		const api = fakeApi();
		const st = new PublishingState(api, 400, 10);
		await st.open(session);
		const run = st.publish();
		await vi.advanceTimersByTimeAsync(20);
		const pub = await run;
		expect(pub?.id).toBe('p1');
		expect(st.publications.s1?.version).toBe(1);
		expect(st.justPublished).toBe(true);
		expect(st.busy).toBeNull();
	});

	it('shows why publishing was refused, without the status code', async () => {
		const api = fakeApi({
			publishSession: vi.fn(async () => {
				throw new Error('409: The audio is on laptop');
			})
		});
		const st = new PublishingState(api, 400, 10);
		await st.open(session);
		expect(await st.publish()).toBeNull();
		expect(st.error).toBe('The audio is on laptop');
		expect(errorText(new Error(''), 'fallback')).toBe('fallback');
	});

	it('makes a file with the password and saves what the job made', async () => {
		const api = fakeApi({
			getJob: vi.fn(async () => job('completed', { token: 't1', filename: 'Waku sync.html', size: 10 }))
		});
		const st = new PublishingState(api, 400, 10);
		await st.open(session);
		st.setTarget('file');
		st.password = 'correct horse';
		const run = st.saveFile();
		await vi.advanceTimersByTimeAsync(20);
		expect(await run).toBe(true);
		expect(api.startPublicationFile.mock.calls[0][2]).toBe('correct horse');
		expect(api.saveFile).toHaveBeenCalledWith('s1', { token: 't1', filename: 'Waku sync.html', size: 10 });
	});
	it('is not saved when the save dialog is cancelled', async () => {
		const api = fakeApi({
			getJob: vi.fn(async () => job('completed', { token: 't1', filename: 'Waku sync.html', size: 10 })),
			saveFile: vi.fn(async () => false)
		});
		const st = new PublishingState(api, 400, 10);
		await st.open(session);
		st.setTarget('file');
		const run = st.saveFile();
		await vi.advanceTimersByTimeAsync(20);
		expect(await run).toBe(false);
		expect(st.error).toBe('');
	});
});

describe('publishing helpers', () => {
	it('lists the meeting people once, without generic speakers, chosen names first', () => {
		expect(meetingPeople(session, ['Grace Hopper'])).toEqual(['Grace Hopper', 'Ada Lovelace', 'Bo']);
	});

	it('merges quotes by span', () => {
		expect(mergeQuotes([span(1, 2)], [span(1, 2, true), span(3, 4)]).map((q) => q.start)).toEqual([1, 3]);
	});

	it('finds the lines and the preview quote of a span, parts included', () => {
		expect(quoteExcerpt(session, span(0, 8))).toBe('The Waku migration is code complete. Great, ship it.');
		expect(quoteExcerpt(session, { start: 0, end: 4, part: 1 })).toBe('Second part line.');
		const quotes = [
			{ id: 'a', text: 'x', speaker: null, start: 0, end: 8, clip: null },
			{ id: 'b', text: 'y', speaker: null, start: 100, end: 104, clip: { preview_url: '/u', seconds: 4, mime: 'audio/ogg' } }
		];
		expect(previewQuoteFor(session, span(0, 8), quotes)?.id).toBe('a');
		expect(previewQuoteFor(session, { start: 0, end: 4, part: 1 }, quotes)?.id).toBe('b');
		expect(previewQuoteFor(session, span(50, 60), quotes)).toBeNull();
	});

	it('offers never only for a file and keeps a saved custom expiry', () => {
		expect(expiryChoices('local', 90)).toEqual([30, 90, 365]);
		expect(expiryChoices('file', 14)).toEqual([14, 30, 90, 365, null]);
		expect(fitExpiry('host', null, 90)).toBe(90);
		expect(fitExpiry('file', null, 90)).toBeNull();
	});

	it('writes short dates in the viewer style', () => {
		const now = new Date(2026, 9, 23);
		expect(shortDate(new Date(2026, 9, 3).toISOString(), now)).toBe('3 Oct');
		expect(shortDate(new Date(2027, 0, 1).toISOString(), now)).toBe('1 Jan 2027');
		expect(shortDate(null, now)).toBe('');
	});

	it('puts the token before a media fragment', () => {
		const saved = connectionState.token;
		connectionState.token = 'tok';
		try {
			expect(backendMediaUrl('/api/audio/file/s1#t=1.00,2.00')).toBe(`${connectionState.url}/api/audio/file/s1?token=tok#t=1.00,2.00`);
		} finally {
			connectionState.token = saved;
		}
	});
});
