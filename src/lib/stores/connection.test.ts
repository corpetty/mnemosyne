import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The store reads localStorage when it is built: load a fresh copy per case.
async function fresh() {
	vi.resetModules();
	return import('./connection.svelte.js');
}

const REMOTE = 'http://127.0.0.1:41234'; // a remote-access tunnel listens on loopback too

beforeEach(() => localStorage.clear());
afterEach(() => vi.unstubAllGlobals());

describe('the local backend keeps its own token', () => {
	it('useLocal after a token was made for it keeps the token', async () => {
		const { connectionState, LOCAL_BACKEND } = await fresh();
		connectionState.save(LOCAL_BACKEND, 'made-by-pairing', false);
		connectionState.useLocal();
		expect(connectionState.url).toBe(LOCAL_BACKEND);
		expect(connectionState.token).toBe('made-by-pairing');
		expect(connectionState.remote).toBe(false);
	});

	it('connecting to another backend and back restores it, also after a restart', async () => {
		const { connectionState, LOCAL_BACKEND } = await fresh();
		connectionState.save(LOCAL_BACKEND, 'local-tok', false);
		connectionState.save(REMOTE, 'main-computer-tok', true); // RemoteConnect.pair
		expect(connectionState.token).toBe('main-computer-tok');
		expect(connectionState.localToken).toBe('local-tok');
		// The window reloads while connected, then the person disconnects.
		const again = (await fresh()).connectionState;
		expect(again.token).toBe('main-computer-tok');
		again.useLocal();
		expect(again.token).toBe('local-tok');
		expect(again.isLocal).toBe(true);
		expect((await fresh()).connectionState.token).toBe('local-tok');
	});

	it('a tunnel reconnect (new port) does not overwrite it', async () => {
		const { connectionState, LOCAL_BACKEND } = await fresh();
		connectionState.save(LOCAL_BACKEND, 'local-tok', false);
		connectionState.save(REMOTE, 'main-tok', true);
		connectionState.save('http://127.0.0.1:41999', connectionState.token, true); // syncRemoteTunnel
		expect(connectionState.localToken).toBe('local-tok');
	});

	it('a token typed for another backend is not taken for the local one', async () => {
		const { connectionState, LOCAL_BACKEND } = await fresh();
		connectionState.save(LOCAL_BACKEND, 'local-tok', false);
		connectionState.save('https://gpu-box.example.org', 'gpu-tok');
		expect(connectionState.localToken).toBe('local-tok');
		connectionState.useLocal();
		expect(connectionState.token).toBe('local-tok');
	});

	it('a window from before the key holds its local token over', async () => {
		localStorage.setItem(
			'mnemosyne.connection',
			JSON.stringify({ url: 'http://127.0.0.1:8008', token: 'old-tok', remote: false })
		);
		const { connectionState } = await fresh();
		expect(connectionState.localToken).toBe('old-tok');
		connectionState.save('http://127.0.0.1:41234', 'main', true);
		connectionState.useLocal();
		expect(connectionState.token).toBe('old-tok');
	});

	it('clearing the token for the local backend (signing out) clears it', async () => {
		const { connectionState, LOCAL_BACKEND } = await fresh();
		connectionState.save(LOCAL_BACKEND, 'member-tok', false);
		connectionState.save(connectionState.url, '', false);
		connectionState.useLocal();
		expect(connectionState.token).toBe('');
	});
});

describe('preparePairing', () => {
	it('stores the token the backend made, and useLocal keeps it', async () => {
		const { connectionState, LOCAL_BACKEND } = await fresh();
		connectionState.save(LOCAL_BACKEND, '', false);
		vi.stubGlobal(
			'fetch',
			vi.fn(async () =>
				new Response(
					JSON.stringify({ new_api_token: 'fresh-token', remote_access: false, api_token_set: true }),
					{ status: 200 }
				)
			)
		);
		const { preparePairing } = await import('../api/backend.js');
		await preparePairing('phone');
		const store = (await import('./connection.svelte.js')).connectionState;
		expect(store.token).toBe('fresh-token');
		store.save(REMOTE, 'main-tok', true);
		store.useLocal();
		expect(store.token).toBe('fresh-token');
	});
});

describe('who owns the personal features', () => {
	const me = (team_mode: boolean, personal: boolean, owner: boolean) =>
		({ team_mode, personal, owner }) as unknown as import('../types/index.js').Me;

	it('everyone on a plain computer, even before /me answers', async () => {
		const { connectionState } = await fresh();
		expect([connectionState.ownsPersonal, connectionState.isOwner]).toEqual([true, true]);
		connectionState.me = me(false, true, true);
		expect([connectionState.ownsPersonal, connectionState.isOwner]).toEqual([true, true]);
	});

	it('only the owner on a computer that hosts a team', async () => {
		const { connectionState } = await fresh();
		connectionState.me = me(true, true, true);
		expect([connectionState.ownsPersonal, connectionState.isOwner]).toEqual([true, true]);
		connectionState.me = me(true, true, false); // a member
		expect([connectionState.ownsPersonal, connectionState.isOwner]).toEqual([false, false]);
	});

	it('nobody on a dedicated team server', async () => {
		const { connectionState } = await fresh();
		connectionState.me = me(true, false, false);
		expect([connectionState.ownsPersonal, connectionState.isOwner]).toEqual([false, false]);
	});
});
