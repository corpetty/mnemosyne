import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { JoinedStatus } from '$lib/types/index.js';

async function fresh() {
	vi.resetModules();
	const connection = await import('./connection.svelte.js');
	const team = await import('./teamMember.svelte.js');
	return { ...connection, ...team };
}

const status = (over: Partial<JoinedStatus> = {}): JoinedStatus => ({
	joined: true,
	host_name: 'Desk',
	user: { id: 'u1', name: 'Ada', role: 'member' },
	reachable: true,
	work: 'here',
	consent_required: false,
	signed_out: false,
	pending_moves: 0,
	...over
});

let current: JoinedStatus;

beforeEach(() => {
	localStorage.clear();
	vi.useFakeTimers();
	current = status();
	vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(current), { status: 200 })));
});
afterEach(() => {
	vi.useRealTimers();
	vi.unstubAllGlobals();
});

describe('teamMemberState', () => {
	it('is not joined before it knows, and sends your own library by default', async () => {
		const { teamMemberState } = await fresh();
		expect(teamMemberState.joined).toBe(false);
		expect(teamMemberState.destination).toBe('mine');
	});

	it('resolves the destination from the library shown and the member\'s work default', async () => {
		const { teamMemberState, connectionState, LOCAL_BACKEND } = await fresh();
		connectionState.save(LOCAL_BACKEND, 't', false);
		await teamMemberState.load();
		expect(teamMemberState.teamName).toBe('Team on Desk');
		expect(teamMemberState.hostName).toBe('Desk');
		expect(teamMemberState.destination).toBe('mine');
		connectionState.useLibrary('team');
		expect(teamMemberState.destination).toBe('team-here');
		current = status({ work: 'host' });
		await vi.advanceTimersByTimeAsync(10_000);
		expect(teamMemberState.destination).toBe('team-host');
	});

	it('a choice in the menu wins over the default until it is cleared', async () => {
		const { teamMemberState, connectionState, LOCAL_BACKEND } = await fresh();
		connectionState.save(LOCAL_BACKEND, 't', false);
		await teamMemberState.load();
		teamMemberState.choose('team-host');
		expect(teamMemberState.destination).toBe('team-host');
		teamMemberState.choose(null);
		expect(teamMemberState.destination).toBe('mine');
	});

	it('polls while joined and stops after leaving', async () => {
		const { teamMemberState } = await fresh();
		await teamMemberState.load();
		const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>;
		const n = fetchMock.mock.calls.length;
		await vi.advanceTimersByTimeAsync(20_000);
		expect(fetchMock.mock.calls.length).toBeGreaterThan(n);
		teamMemberState.apply(status({ joined: false, host_name: null, user: null }));
		const m = fetchMock.mock.calls.length;
		await vi.advanceTimersByTimeAsync(30_000);
		expect(fetchMock.mock.calls.length).toBe(m);
		expect(teamMemberState.joined).toBe(false);
		expect(teamMemberState.destination).toBe('mine');
	});

	it('carries the host\'s consent rule, signed-out state and the pending count', async () => {
		current = status({ consent_required: true, signed_out: true, reachable: false, pending_moves: 2 });
		const { teamMemberState } = await fresh();
		await teamMemberState.load();
		expect(teamMemberState.hostConsentRequired).toBe(true);
		expect(teamMemberState.signedOut).toBe(true);
		expect(teamMemberState.reachable).toBe(false);
		expect(teamMemberState.pending).toBe(2);
	});

	it('keeps what it knew when the backend does not answer', async () => {
		const { teamMemberState } = await fresh();
		await teamMemberState.load();
		vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('down'); }));
		await vi.advanceTimersByTimeAsync(10_000);
		expect(teamMemberState.joined).toBe(true);
		teamMemberState.stop();
	});
});

describe('before the backend answers', () => {
	it('asks again until it hears', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('starting'); }));
		const { teamMemberState } = await fresh();
		await teamMemberState.load();
		expect(teamMemberState.joined).toBe(false);
		vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(status()), { status: 200 })));
		await vi.advanceTimersByTimeAsync(3000);
		expect(teamMemberState.joined).toBe(true);
		teamMemberState.stop();
	});
});

describe('meetings that arrive in the team', () => {
	const moved = (id: string, host: string) => ({ session_id: id, host_session_id: host, name: id, at: '2026-11-10T10:00:00+00:00' });
	let movesAnswer: { pending: unknown[]; moved: ReturnType<typeof moved>[] };

	beforeEach(() => {
		movesAnswer = { pending: [], moved: [moved('old', 'h-old')] };
		vi.stubGlobal(
			'fetch',
			vi.fn(async (url: string) =>
				url.endsWith('/api/joined/moves')
					? new Response(JSON.stringify(movesAnswer))
					: new Response(JSON.stringify(current))
			)
		);
	});

	it('tells of a move that finished after the first look, once, and not of earlier ones', async () => {
		const { teamMemberState } = await fresh();
		const seen: string[] = [];
		teamMemberState.onMoved = (m) => seen.push(m.host_session_id);
		current = status({ pending_moves: 1 });
		await teamMemberState.load(); // first look: 'old' is what had moved before
		expect(seen).toEqual([]);
		movesAnswer = { pending: [], moved: [moved('new', 'h-new'), moved('old', 'h-old')] };
		current = status({ pending_moves: 0 });
		await vi.advanceTimersByTimeAsync(10_000);
		expect(seen).toEqual(['h-new']);
		await vi.advanceTimersByTimeAsync(30_000);
		expect(seen).toEqual(['h-new']);
		teamMemberState.stop();
	});

	it('looks every two seconds for a while after a meeting was put in the team', async () => {
		const { teamMemberState } = await fresh();
		const seen: string[] = [];
		teamMemberState.onMoved = (m) => seen.push(m.host_session_id);
		current = status({ pending_moves: 1 });
		await teamMemberState.load();
		teamMemberState.expectMove();
		movesAnswer = { pending: [], moved: [moved('new', 'h-new'), moved('old', 'h-old')] };
		current = status({ pending_moves: 0 });
		await vi.advanceTimersByTimeAsync(2000);
		expect(seen).toEqual(['h-new']);
		teamMemberState.stop();
	});
});
