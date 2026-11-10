import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The stores read localStorage when they are built: load fresh copies per case.
async function fresh() {
	vi.resetModules();
	const connection = await import('../stores/connection.svelte.js');
	const local = await import('./local.js');
	const backend = await import('./backend.js');
	return { ...connection, local, backend };
}

type Call = { url: string; method: string; auth: string | null; body: unknown };
let calls: Call[] = [];

/** A fake fetch: `answer(path, method)` gives the body of a 200, or a number for that status. */
function fakeFetch(answer: (path: string, method: string) => unknown) {
	calls = [];
	vi.stubGlobal(
		'fetch',
		vi.fn(async (url: string, init?: RequestInit) => {
			const headers = (init?.headers ?? {}) as Record<string, string>;
			const method = init?.method ?? 'GET';
			calls.push({
				url,
				method,
				auth: headers.Authorization ?? null,
				body: init?.body ? JSON.parse(init.body as string) : undefined
			});
			const path = url.replace(/^https?:\/\/[^/]+/, '');
			const a = answer(path, method);
			if (typeof a === 'number') return new Response(JSON.stringify({ detail: `status ${a}` }), { status: a });
			return new Response(JSON.stringify(a), { status: 200 });
		})
	);
}

const MAIN = 'http://127.0.0.1:41234'; // the remote-access tunnel to the main computer

beforeEach(() => localStorage.clear());
afterEach(() => vi.unstubAllGlobals());

describe('the local client', () => {
	it('talks to this computer with its own token while the window is connected to the main computer', async () => {
		const { connectionState, LOCAL_BACKEND, local } = await fresh();
		connectionState.save(LOCAL_BACKEND, 'local-tok', false);
		connectionState.save(MAIN, 'main-tok', true);
		fakeFetch(() => ({ main: null, reachable: false, recording: null, outbox: [] }));
		await local.getSatellite();
		expect(calls[0].url).toBe(`${LOCAL_BACKEND}/api/satellite`);
		expect(calls[0].auth).toBe('Bearer local-tok');
	});

	it('sends no Authorization header when the local backend has no token', async () => {
		const { local } = await fresh();
		fakeFetch(() => ({}));
		await local.getSatellite();
		expect(calls[0].auth).toBeNull();
	});

	it('start and stop post to the satellite routes, and the outbox calls name the item', async () => {
		const { local } = await fresh();
		fakeFetch((path) =>
			path.endsWith('/start')
				? { session_id: 's1', local_id: 'abc', streaming: true }
				: { local_id: 'abc', session_id: 's1', outcome: 'streamed' }
		);
		const started = await local.startSatellite({ mic_device: 5, system_device: 7, consent: 'all_parties' });
		expect(started.streaming).toBe(true);
		expect(calls[0]).toMatchObject({
			method: 'POST',
			url: expect.stringContaining('/api/satellite/start'),
			body: { mic_device: 5, system_device: 7, consent: 'all_parties' }
		});
		await local.stopSatellite('notes');
		expect(calls[1].body).toEqual({ notes: 'notes' });
		await local.outboxAsNewMeeting('a/b');
		expect(calls[2].url).toContain('/api/satellite/outbox/a%2Fb/new-meeting');
		await local.outboxDiscard('abc');
		expect(calls[3]).toMatchObject({ method: 'DELETE' });
	});

	it('a start can name the team as its destination', async () => {
		const { local } = await fresh();
		fakeFetch(() => ({ session_id: null, local_id: 'abc', streaming: false }));
		await local.startSatellite({ mic_device: 5, system_device: null, destination: 'team' });
		expect(calls[0].body).toEqual({ mic_device: 5, system_device: null, destination: 'team' });
	});

	it('an error carries the status and the backend detail', async () => {
		const { local } = await fresh();
		fakeFetch(() => 409);
		await expect(local.stopSatellite()).rejects.toThrow('409: status 409');
	});

	it('device calls go to this computer when the window uses a main computer, else to the connected backend', async () => {
		const { connectionState, LOCAL_BACKEND, backend } = await fresh();
		connectionState.save(LOCAL_BACKEND, 'local-tok', false);
		fakeFetch(() => []);
		await backend.getDevices();
		expect(calls[0].url).toBe(`${LOCAL_BACKEND}/api/devices`);
		connectionState.save(MAIN, 'main-tok', true);
		await backend.getDevices();
		await backend.getDeviceLevel(3, 1.5);
		await backend.getEchoCancel();
		expect(calls.slice(1).map((c) => c.url)).toEqual([
			`${LOCAL_BACKEND}/api/devices`,
			`${LOCAL_BACKEND}/api/audio/level/3?seconds=1.5`,
			`${LOCAL_BACKEND}/api/audio/echo-cancel`
		]);
		expect(calls.slice(1).every((c) => c.auth === 'Bearer local-tok')).toBe(true);
		// The main computer's own backend is never asked for a device while connected remotely.
		connectionState.save(MAIN, 'main-tok', false); // not marked remote: this IS the connected backend
		calls.length = 0;
		await backend.getDevices();
		expect(calls[0].url).toBe(`${MAIN}/api/devices`);
	});

	it('syncing for a library copy keeps a token the local backend made for itself', async () => {
		const { connectionState, LOCAL_BACKEND, local } = await fresh();
		connectionState.save(MAIN, 'main-tok', true);
		fakeFetch((path) =>
			path === '/api/pairing/prepare'
				? { new_api_token: 'made-here' }
				: { peer: { id: 'p1', name: 'Desktop' }, person_error: null }
		);
		const r = await local.localSyncPair('ticket#code');
		expect(r.peer.id).toBe('p1');
		expect(calls.map((c) => c.url.replace(LOCAL_BACKEND, ''))).toEqual(['/api/pairing/prepare', '/api/sync/pair']);
		expect(connectionState.localToken).toBe('made-here');
		expect(connectionState.token).toBe('main-tok'); // the main connection is untouched
		expect(calls[1].auth).toBe('Bearer made-here');
		expect((await fresh()).connectionState.localToken).toBe('made-here');
	});
});
