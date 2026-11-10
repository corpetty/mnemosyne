import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { JoinedStatus } from '$lib/types/index.js';

// The controller builds effect roots and talks to many stores: load a fresh copy per case.
async function fresh() {
	vi.resetModules();
	const connection = await import('../stores/connection.svelte.js');
	const team = await import('../stores/teamMember.svelte.js');
	const ui = await import('../stores/ui.svelte.js');
	const audio = await import('../stores/audio.svelte.js');
	const controller = await import('./controller.svelte.js');
	const destination = await import('./teamDestination.js');
	return { ...connection, ...team, ...ui, ...audio, ...controller, ...destination };
}

const joined = (over: Partial<JoinedStatus> = {}): JoinedStatus => ({
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

let status: JoinedStatus;
let requests: { path: string; method: string }[];
let reload: ReturnType<typeof vi.fn>;

beforeEach(() => {
	localStorage.clear();
	sessionStorage.clear();
	requests = [];
	status = joined();
	reload = vi.fn();
	vi.stubGlobal('location', { reload, origin: 'http://localhost', search: '', pathname: '/', hash: '' });
	vi.stubGlobal(
		'fetch',
		vi.fn(async (url: string, init?: RequestInit) => {
			const path = url.replace(/^https?:\/\/[^/]+/, '');
			requests.push({ path, method: init?.method ?? 'GET' });
			if (path === '/api/joined') return new Response(JSON.stringify(status));
			return new Response('{}');
		})
	);
});
afterEach(() => {
	vi.unstubAllGlobals();
});

describe('Record when this computer has joined a team', () => {
	it('streaming to the team from your library switches to the team library first, and says what to start', async () => {
		const c = await fresh();
		c.connectionState.save(c.LOCAL_BACKEND, 'tok', false);
		await c.teamMemberState.load();
		c.teamMemberState.choose('team-host');
		await c.startRecording(true);
		expect(reload).toHaveBeenCalledOnce();
		expect(c.connectionState.library).toBe('team');
		expect(c.takePendingStart()).toEqual({ destination: 'team-host', fresh: true });
		expect(requests.some((r) => r.path.startsWith('/api/satellite/start'))).toBe(false);
	});

	it('doing the work here, started from the team library, switches to your library (where it is recorded)', async () => {
		const c = await fresh();
		c.connectionState.save(c.LOCAL_BACKEND, 'tok', false);
		c.connectionState.useLibrary('team');
		await c.teamMemberState.load(); // work = here: the default in the team library
		expect(c.teamMemberState.destination).toBe('team-here');
		await c.startRecording();
		expect(reload).toHaveBeenCalledOnce();
		expect(c.connectionState.library).toBe('mine');
		expect(c.takePendingStart()).toEqual({ destination: 'team-here', fresh: false });
	});

	it('asks about consent by the HOST\'s rule for a team destination, not the connected backend\'s', async () => {
		const c = await fresh();
		status = joined({ consent_required: true });
		c.connectionState.save(c.LOCAL_BACKEND, 'tok', false);
		c.connectionState.useLibrary('team');
		c.connectionState.consentRequired = false; // what the proxied /health said
		await c.teamMemberState.load();
		c.teamMemberState.choose('team-host');
		await c.startRecording(true);
		expect(c.uiState.consentAsk).toEqual({ fresh: true });
		expect(reload).not.toHaveBeenCalled();
		expect(requests.some((r) => r.path.startsWith('/api/satellite/start'))).toBe(false);
	});

	it('does not ask when the host does not, whatever the local rule says', async () => {
		const c = await fresh();
		c.connectionState.save(c.LOCAL_BACKEND, 'tok', false);
		c.connectionState.useLibrary('team');
		c.connectionState.consentRequired = true;
		await c.teamMemberState.load();
		c.teamMemberState.choose('team-host');
		await c.startRecording(true);
		expect(c.uiState.consentAsk).toBeNull();
		// It went on to start: this computer's devices were asked for (none chosen, so it stops there).
		expect(requests.some((r) => r.path === '/api/devices')).toBe(true);
	});

	it('your own library keeps the connected backend\'s rule', async () => {
		const c = await fresh();
		c.connectionState.save(c.LOCAL_BACKEND, 'tok', false);
		c.connectionState.consentRequired = true;
		status = joined({ consent_required: false });
		await c.teamMemberState.load();
		await c.startRecording(true);
		expect(c.uiState.consentAsk).toEqual({ fresh: true });
	});

	it('a computer that has joined nothing behaves as before', async () => {
		const c = await fresh();
		status = joined({ joined: false, host_name: null, user: null });
		c.connectionState.save(c.LOCAL_BACKEND, 'tok', false);
		c.connectionState.consentRequired = true;
		await c.teamMemberState.load();
		await c.startRecording(true);
		expect(c.uiState.consentAsk).toEqual({ fresh: true });
		expect(reload).not.toHaveBeenCalled();
	});

	it('does not switch library while a recording is running', async () => {
		const c = await fresh();
		c.connectionState.save(c.LOCAL_BACKEND, 'tok', false);
		c.audioState.isRecording = true;
		expect(c.switchLibrary('team')).toBe(false);
		expect(reload).not.toHaveBeenCalled();
		expect(c.connectionState.library).toBe('mine');
	});
});
