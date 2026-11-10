import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

async function fresh() {
	vi.resetModules();
	const connection = await import('./connection.svelte.js');
	const audio = await import('./audio.svelte.js');
	return { ...connection, ...audio };
}

const dev = (id: number, over: Record<string, unknown> = {}) => ({
	id,
	name: `dev${id}`,
	description: `Device ${id}`,
	media_class: 'Audio/Source',
	is_input: true,
	is_output: false,
	is_monitor: false,
	is_echo_cancelled: false,
	...over
});

const DEVICES = [
	dev(1),
	dev(2, { is_monitor: true }),
	dev(3, { media_class: 'Audio/Sink', is_input: false, is_output: true })
];

let posted: { path: string; body: unknown }[];
let startAnswer: unknown;

beforeEach(() => {
	localStorage.clear();
	posted = [];
	startAnswer = { session_id: 'sess1', local_id: 'loc1', streaming: true };
	vi.stubGlobal(
		'fetch',
		vi.fn(async (url: string, init?: RequestInit) => {
			const path = url.replace(/^https?:\/\/[^/]+/, '');
			if (init?.method === 'POST') posted.push({ path, body: JSON.parse(init.body as string) });
			if (path === '/api/devices') return new Response(JSON.stringify(DEVICES));
			if (path === '/api/satellite/start') {
				if (startAnswer === 409) return new Response(JSON.stringify({ detail: 'This computer is already recording' }), { status: 409 });
				return new Response(JSON.stringify(startAnswer));
			}
			if (path === '/api/satellite/stop')
				return new Response(JSON.stringify({ local_id: 'loc1', session_id: 'sess1', outcome: 'streamed' }));
			return new Response('{}');
		})
	);
});
afterEach(() => {
	vi.unstubAllGlobals();
});

describe('recording through this computer', () => {
	it('lists this computer\'s devices and starts with the first microphone and the first call output', async () => {
		const { audioState, connectionState, LOCAL_BACKEND } = await fresh();
		connectionState.save(LOCAL_BACKEND, 'local-tok', false);
		connectionState.save('http://127.0.0.1:41234', 'main-tok', true);
		await audioState.loadDevices();
		expect(audioState.devices.map((d) => d.id)).toEqual([1, 2, 3]);
		audioState.toggleDevice(1);
		audioState.toggleDevice(3);
		const res = await audioState.startSatellite('all_parties');
		expect(res?.session_id).toBe('sess1');
		expect(posted.at(-1)).toEqual({
			path: '/api/satellite/start',
			body: { mic_device: 1, system_device: 3, consent: 'all_parties' }
		});
		expect(audioState.isRecording).toBe(true);
		expect(audioState.satellite).toBe(true);
		expect(audioState.localOnly).toBe(false);
		expect(audioState.activeSessionId).toBe('sess1');
		expect(audioState.satelliteId).toBe('loc1');
		const stopped = await audioState.stopSatellite();
		expect(stopped?.outcome).toBe('streamed');
		expect(audioState.isRecording).toBe(false);
		expect(audioState.satellite).toBe(false);
		expect(audioState.activeSessionId).toBeNull();
	});

	it('a recording the main computer is not receiving is local only, with no meeting yet', async () => {
		const { audioState } = await fresh();
		startAnswer = { session_id: null, local_id: 'loc2', streaming: false };
		await audioState.loadDevices();
		audioState.toggleDevice(2); // a monitor counts as call audio
		await audioState.startSatellite();
		expect(posted.at(-1)?.body).toEqual({ mic_device: null, system_device: 2, consent: null });
		expect(audioState.localOnly).toBe(true);
		expect(audioState.activeSessionId).toBeNull();
		expect(audioState.isRecording).toBe(true);
	});

	it('passes the destination it is given (the team) and none otherwise', async () => {
		const { audioState } = await fresh();
		await audioState.loadDevices();
		audioState.toggleDevice(1);
		await audioState.startSatellite(null, 'team');
		expect(posted.at(-1)?.body).toEqual({
			mic_device: 1,
			system_device: null,
			consent: null,
			destination: 'team'
		});
	});

	it('asks for a device first, and says why a start was refused', async () => {
		const { audioState } = await fresh();
		await audioState.loadDevices();
		expect(await audioState.startSatellite()).toBeUndefined();
		expect(audioState.error).toBe('Select a microphone and/or call audio');
		expect(posted).toEqual([]);
		audioState.toggleDevice(1);
		startAnswer = 409;
		expect(await audioState.startSatellite()).toBeUndefined();
		expect(audioState.error).toBe('This computer is already recording');
		expect(audioState.isRecording).toBe(false);
		expect(audioState.pending).toBeNull();
	});

	it('picks up a recording the local backend is making (the window was reopened)', async () => {
		const { audioState } = await fresh();
		audioState.resumeSatellite({
			local_id: 'loc3',
			session_id: 'sess3',
			destination: 'main',
			streaming: true,
			title: '',
			started_at: new Date(Date.now() - 65_000).toISOString(),
			sources: []
		});
		expect(audioState.isRecording).toBe(true);
		expect(audioState.activeSessionId).toBe('sess3');
		expect(audioState.recordingDuration).toBeGreaterThanOrEqual(65);
		audioState.ended();
		expect(audioState.satellite).toBe(false);
	});
});

describe('recording while the window shows the team library', () => {
	it('lists this computer\'s devices and streams to the team, and remembers where it goes', async () => {
		const { audioState, connectionState, LOCAL_BACKEND } = await fresh();
		connectionState.save(LOCAL_BACKEND, 'local-tok', false);
		connectionState.useLibrary('team');
		await audioState.loadDevices();
		expect(audioState.devices.map((d) => d.id)).toEqual([1, 2, 3]); // the local backend's, not the host's
		audioState.toggleDevice(1);
		await audioState.startSatellite('one_party', 'team');
		expect(posted.at(-1)).toEqual({
			path: '/api/satellite/start',
			body: { mic_device: 1, system_device: null, consent: 'one_party', destination: 'team' }
		});
		expect(audioState.satelliteDestination).toBe('team');
		await audioState.stopSatellite();
		expect(audioState.satelliteDestination).toBe('main');
	});

	it('a recording made here for the team marks the start with for_team', async () => {
		const { audioState, connectionState, LOCAL_BACKEND } = await fresh();
		connectionState.save(LOCAL_BACKEND, 'local-tok', false);
		(fetch as unknown as ReturnType<typeof vi.fn>).mockImplementationOnce(
			async (url: string, init?: RequestInit) => {
				posted.push({ path: url.replace(/^https?:\/\/[^/]+/, ''), body: JSON.parse(init!.body as string) });
				return new Response(JSON.stringify({ session_id: 's9', recording_id: 'r', status: 'recording' }));
			}
		);
		audioState.selectedDeviceIds = new Set([1]);
		await audioState.startRecording('s9', 'all_parties', true);
		expect(posted[0].path).toBe('/api/audio/start');
		expect(posted[0].body).toMatchObject({ device_ids: [1], session_id: 's9', for_team: true });
	});

	it('an ordinary start does not mention the team', async () => {
		const { audioState, connectionState, LOCAL_BACKEND } = await fresh();
		connectionState.save(LOCAL_BACKEND, 'local-tok', false);
		(fetch as unknown as ReturnType<typeof vi.fn>).mockImplementationOnce(
			async (url: string, init?: RequestInit) => {
				posted.push({ path: url.replace(/^https?:\/\/[^/]+/, ''), body: JSON.parse(init!.body as string) });
				return new Response(JSON.stringify({ session_id: 's9', recording_id: 'r', status: 'recording' }));
			}
		);
		audioState.selectedDeviceIds = new Set([1]);
		await audioState.startRecording('s9');
		expect(posted[0].body).not.toHaveProperty('for_team');
	});
});
