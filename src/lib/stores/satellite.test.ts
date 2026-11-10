import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { SatelliteStatus } from '$lib/types/index.js';

async function fresh() {
	vi.resetModules();
	const connection = await import('./connection.svelte.js');
	const sat = await import('./satellite.svelte.js');
	return { ...connection, ...sat };
}

function status(over: Partial<SatelliteStatus> = {}): SatelliteStatus {
	return {
		main: { name: 'Desktop' },
		reachable: true,
		team: null,
		team_reachable: false,
		recording: null,
		outbox: [],
		...over
	};
}

const entry = (
	id: string,
	state: 'recording' | 'waiting' | 'sending' | 'failed',
	destination: 'main' | 'team' = 'main'
) => ({
	id,
	title: id,
	started_at: '2026-11-09T10:00:00+00:00',
	parts: 1,
	state,
	last_error: state === 'failed' ? 'refused' : '',
	replaces: null,
	destination
});

let current: SatelliteStatus | 'down';
let polls: number;

beforeEach(() => {
	localStorage.clear();
	vi.useFakeTimers();
	polls = 0;
	current = status();
	vi.stubGlobal(
		'fetch',
		vi.fn(async () => {
			polls++;
			if (current === 'down') throw new TypeError('fetch failed');
			return new Response(JSON.stringify(current), { status: 200 });
		})
	);
});
afterEach(() => {
	vi.useRealTimers();
	vi.unstubAllGlobals();
});

describe('satelliteState', () => {
	it('polls every 2 s in the uses-main role', async () => {
		const { satelliteState, POLL_MS } = await fresh();
		satelliteState.follow(true);
		await vi.advanceTimersByTimeAsync(0);
		expect(polls).toBe(1);
		expect(satelliteState.localUp).toBe(true);
		expect(satelliteState.mainName).toBe('Desktop');
		await vi.advanceTimersByTimeAsync(POLL_MS * 3);
		expect(polls).toBe(4);
		satelliteState.stop();
		await vi.advanceTimersByTimeAsync(POLL_MS * 3);
		expect(polls).toBe(4);
	});

	it('says the main computer is down only in the uses-main role, from the local backend', async () => {
		const { satelliteState } = await fresh();
		current = status({ reachable: false });
		satelliteState.follow(true);
		await vi.advanceTimersByTimeAsync(0);
		expect(satelliteState.mainDown).toBe(true);
		current = status({ reachable: true });
		await vi.advanceTimersByTimeAsync(2000);
		expect(satelliteState.mainDown).toBe(false);
		satelliteState.stop();
	});

	it('lists what waits, not what is being recorded', async () => {
		const { satelliteState } = await fresh();
		current = status({
			outbox: [entry('a', 'recording'), entry('b', 'waiting'), entry('c', 'sending'), entry('d', 'failed')]
		});
		satelliteState.follow(true);
		await vi.advanceTimersByTimeAsync(0);
		expect(satelliteState.waiting.map((e) => e.id)).toEqual(['b', 'c', 'd']);
		satelliteState.stop();
	});

	it('survives one failed poll and counts the local backend as gone after two', async () => {
		const { satelliteState, POLL_MS } = await fresh();
		satelliteState.follow(true);
		await vi.advanceTimersByTimeAsync(0);
		expect(satelliteState.localUp).toBe(true);
		current = 'down';
		await vi.advanceTimersByTimeAsync(POLL_MS);
		expect(satelliteState.localUp).toBe(true);
		expect(satelliteState.error).toContain('fetch failed');
		await vi.advanceTimersByTimeAsync(POLL_MS);
		expect(satelliteState.localUp).toBe(false);
		expect(satelliteState.mainDown).toBe(false); // unknown is not "down"
		current = status();
		await vi.advanceTimersByTimeAsync(POLL_MS);
		expect(satelliteState.localUp).toBe(true);
		expect(satelliteState.error).toBe('');
		satelliteState.stop();
	});

	it('outside the role it looks once and stops unless something is waiting', async () => {
		const { satelliteState, connectionState, LOCAL_BACKEND, POLL_MS } = await fresh();
		connectionState.save(LOCAL_BACKEND, 'tok', false);
		current = status({ main: null });
		satelliteState.follow(false);
		await vi.advanceTimersByTimeAsync(POLL_MS * 5);
		expect(polls).toBe(1);

		// An item still in the outbox (the window was disconnected from the main computer): keep looking.
		const again = await fresh();
		again.connectionState.save(LOCAL_BACKEND, 'tok', false);
		polls = 0;
		current = status({ main: null, outbox: [entry('x', 'waiting')] });
		again.satelliteState.follow(false);
		await vi.advanceTimersByTimeAsync(POLL_MS * 3);
		expect(polls).toBeGreaterThan(2);
		current = status({ main: null, outbox: [] });
		await vi.advanceTimersByTimeAsync(POLL_MS * 2);
		const n = polls;
		await vi.advanceTimersByTimeAsync(POLL_MS * 5);
		expect(polls).toBe(n);
		again.satelliteState.stop();
	});

	it('exposes the team beside the main computer, and what waits for each', async () => {
		const { satelliteState } = await fresh();
		current = status({
			main: null,
			reachable: false,
			team: { name: 'Acme' },
			team_reachable: false,
			outbox: [entry('a', 'waiting', 'team'), entry('b', 'waiting', 'main'), entry('c', 'recording', 'team')]
		});
		satelliteState.follow(true);
		await vi.advanceTimersByTimeAsync(0);
		expect(satelliteState.inTeam).toBe(true);
		expect(satelliteState.teamName).toBe('Acme');
		expect(satelliteState.teamDown).toBe(true);
		expect(satelliteState.mainDown).toBe(false); // no main computer: nothing to be down
		expect(satelliteState.waitingFor('team').map((e) => e.id)).toEqual(['a']);
		expect(satelliteState.waitingFor('main').map((e) => e.id)).toEqual(['b']);
		current = status({ main: null, team: { name: 'Acme' }, team_reachable: true });
		await vi.advanceTimersByTimeAsync(2000);
		expect(satelliteState.teamDown).toBe(false);
		satelliteState.stop();
	});

	it('is not in a team without one in the status', async () => {
		const { satelliteState } = await fresh();
		satelliteState.follow(true);
		await vi.advanceTimersByTimeAsync(0);
		expect(satelliteState.inTeam).toBe(false);
		expect(satelliteState.teamDown).toBe(false);
		satelliteState.stop();
	});

	it('still looks at the local outbox in the team library (the local backend under /api/joined)', async () => {
		const { satelliteState, connectionState, LOCAL_BACKEND, onLocalBackend } = await fresh();
		expect(onLocalBackend(LOCAL_BACKEND)).toBe(true);
		expect(onLocalBackend(`${LOCAL_BACKEND}/api/joined`)).toBe(true);
		expect(onLocalBackend(`${LOCAL_BACKEND}/api/joined/`)).toBe(true);
		expect(onLocalBackend(`${LOCAL_BACKEND}/api/other`)).toBe(false);
		expect(onLocalBackend('http://192.168.1.5:8008/api/joined')).toBe(false);
		connectionState.save(`${LOCAL_BACKEND}/api/joined`, 'tok', false);
		current = status({ main: null, outbox: [entry('x', 'waiting', 'team')] });
		satelliteState.follow(false);
		await vi.advanceTimersByTimeAsync(0);
		expect(polls).toBe(1);
		expect(satelliteState.waitingFor('team')).toHaveLength(1);
		satelliteState.stop();
	});

	it('does not ask a backend that is not this computer for an outbox', async () => {
		const { satelliteState, connectionState } = await fresh();
		connectionState.save('http://192.168.1.5:8008', 'tok', false);
		satelliteState.follow(false);
		await vi.advanceTimersByTimeAsync(5000);
		expect(polls).toBe(0);
	});

	it('shows the recording and shares a poll already on its way', async () => {
		const { satelliteState } = await fresh();
		current = status({
			recording: {
				local_id: 'r1',
				session_id: null,
				destination: 'main',
				streaming: false,
				title: '',
				started_at: '2026-11-09T10:00:00+00:00',
				sources: []
			}
		});
		const a = satelliteState.refresh();
		const b = satelliteState.refresh();
		await Promise.all([a, b]);
		expect(polls).toBe(1);
		expect(satelliteState.recording?.local_id).toBe('r1');
	});
});
