import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { JoinedStatus } from '$lib/types/index.js';

// The controller builds effect roots and talks to many stores: load a fresh copy per case.
async function fresh() {
	vi.resetModules();
	const connection = await import('../stores/connection.svelte.js');
	const team = await import('../stores/teamMember.svelte.js');
	const ui = await import('../stores/ui.svelte.js');
	const audio = await import('../stores/audio.svelte.js');
	const session = await import('../stores/session.svelte.js');
	const controller = await import('./controller.svelte.js');
	const destination = await import('./teamDestination.js');
	return { ...connection, ...team, ...ui, ...audio, ...session, ...controller, ...destination };
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
let requests: { path: string; method: string; body?: unknown }[];
let stored: Record<string, unknown>; // meetings the fake backend has, by id
let reload: ReturnType<typeof vi.fn>;

beforeEach(() => {
	localStorage.clear();
	sessionStorage.clear();
	requests = [];
	stored = {};
	status = joined();
	reload = vi.fn();
	vi.stubGlobal('location', { reload, origin: 'http://localhost', search: '', pathname: '/', hash: '' });
	vi.stubGlobal(
		'fetch',
		vi.fn(async (url: string, init?: RequestInit) => {
			const path = url.replace(/^https?:\/\/[^/]+/, '');
			requests.push({ path, method: init?.method ?? 'GET', body: init?.body ? JSON.parse(init.body as string) : undefined });
			if (path === '/api/joined') return new Response(JSON.stringify(status));
			const m = path.match(/^(?:\/api\/joined)?\/api\/sessions\/(\w+)$/);
			if (m && stored[m[1]]) return new Response(JSON.stringify(init?.method === 'DELETE' ? {} : stored[m[1]]));
			if (path === '/api/devices')
				return new Response(
					JSON.stringify([{ id: 1, name: 'mic', description: 'Mic', media_class: 'Audio/Source', is_input: true, is_output: false, is_monitor: false, is_echo_cancelled: false }])
				);
			if (path === '/api/satellite/start')
				return new Response(JSON.stringify({ session_id: 'd1', local_id: 'l1', streaming: true }));
			if (path === '/api/satellite') return new Response('{}', { status: 500 });
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

describe('the empty draft left open by "New meeting"', () => {
	const draft = (over: Record<string, unknown> = {}) => ({
		id: 'd1',
		name: 'Untitled Session',
		status: 'created',
		transcript: [],
		recordings: [],
		notes: '',
		...over
	});

	it('is removed when the recording goes to the other library', async () => {
		const c = await fresh();
		stored.d1 = draft();
		c.connectionState.save(c.LOCAL_BACKEND, 'tok', false);
		await c.teamMemberState.load();
		c.sessionState.show(stored.d1 as never);
		c.teamMemberState.choose('team-host');
		await c.startRecording(true);
		expect(requests.some((r) => r.method === 'DELETE' && r.path === '/api/sessions/d1')).toBe(true);
		expect(reload).toHaveBeenCalledOnce();
		expect(c.sessionState.activeSession).toBeNull();
	});

	it('is kept when it has something in it', async () => {
		const c = await fresh();
		stored.d1 = draft({ notes: 'agenda for later' });
		c.connectionState.save(c.LOCAL_BACKEND, 'tok', false);
		await c.teamMemberState.load();
		c.sessionState.show(stored.d1 as never);
		c.teamMemberState.choose('team-host');
		await c.startRecording(true);
		expect(requests.some((r) => r.method === 'DELETE')).toBe(false);
		expect(reload).toHaveBeenCalledOnce();
	});

	it('is looked at again before it goes: notes typed since it was opened keep it', async () => {
		const c = await fresh();
		stored.d1 = draft({ notes: 'typed meanwhile' });
		c.connectionState.save(c.LOCAL_BACKEND, 'tok', false);
		await c.teamMemberState.load();
		c.sessionState.show(draft() as never); // what the window loaded: still empty
		c.teamMemberState.choose('team-host');
		await c.startRecording(true);
		expect(requests.some((r) => r.method === 'DELETE')).toBe(false);
	});

	it('is recorded into when the team\'s host does the work from the team library', async () => {
		const c = await fresh();
		stored.d1 = draft();
		c.connectionState.save(c.LOCAL_BACKEND, 'tok', false);
		c.connectionState.useLibrary('team');
		await c.teamMemberState.load();
		c.sessionState.show(stored.d1 as never);
		c.teamMemberState.choose('team-host');
		c.audioState.selectedDeviceIds = new Set([1]);
		await c.startRecording(true);
		const start = requests.find((r) => r.path === '/api/satellite/start');
		expect(start?.body).toMatchObject({ destination: 'team', session_id: 'd1' });
		expect(requests.some((r) => r.method === 'DELETE')).toBe(false);
		expect(reload).not.toHaveBeenCalled();
	});

	it('a team meeting with content is not recorded into: the host makes a new one', async () => {
		const c = await fresh();
		stored.t1 = draft({ id: 't1', name: 'Team standup', transcript: [{}] });
		c.connectionState.save(c.LOCAL_BACKEND, 'tok', false);
		c.connectionState.useLibrary('team');
		await c.teamMemberState.load();
		c.sessionState.show(stored.t1 as never);
		c.teamMemberState.choose('team-host');
		c.audioState.selectedDeviceIds = new Set([1]);
		await c.startRecording(true);
		const start = requests.find((r) => r.path === '/api/satellite/start');
		expect(start?.body).not.toHaveProperty('session_id');
	});
});

describe('a meeting that arrived in the team', () => {
	it('refreshes the list, closes the open copy and offers to open the team\'s', async () => {
		const c = await fresh();
		const toast = await import('../stores/toast.svelte.js');
		c.connectionState.save(c.LOCAL_BACKEND, 'tok', false);
		await c.teamMemberState.load();
		c.sessionState.show({ id: 'local1', name: 'Local planning' } as never);
		c.teamMemberState.onMoved!({ session_id: 'local1', host_session_id: 'host1', name: 'Local planning', at: '' });
		expect(c.sessionState.activeSession).toBeNull();
		expect(requests.some((r) => r.path === '/api/sessions')).toBe(true);
		const t = toast.toastState.toasts.at(-1)!;
		expect(t.message).toBe('Moved to Team on Desk');
		t.action!.run();
		await Promise.resolve();
		// Opening it switches to the team library, and leaves a note for the reloaded window.
		expect(reload).toHaveBeenCalledOnce();
		expect(c.connectionState.library).toBe('team');
		expect(c.takeOpenAfterSwitch()).toBe('host1');
	});

	it('another open meeting stays open', async () => {
		const c = await fresh();
		c.connectionState.save(c.LOCAL_BACKEND, 'tok', false);
		await c.teamMemberState.load();
		c.sessionState.show({ id: 'other', name: 'Other' } as never);
		c.teamMemberState.onMoved!({ session_id: 'local1', host_session_id: 'host1', name: '', at: '' });
		expect(c.sessionState.activeSession?.id).toBe('other');
	});
});
