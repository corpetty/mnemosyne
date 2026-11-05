// @vitest-environment node
// The live viewer against a scripted host: a fake fetch for /pub/<id> and a fake WebSocket for
// the room (the host's side is played by the test), with real WebCrypto (Node's) and the
// committed vectors of publication_live_v1.json.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { RoomSocket } from '$lib/rooms/client';
import { deriveLiveKeys, PubKind, signFrame } from '$lib/sealed/signed';
import { b64urlDecode, blobHash, fromHex, parseManifest, type Manifest } from './format';
import { Follower, POLL_MS, STATUS, statusLine, type Gone, type Shown } from './live';
import { openHosted, type Ready } from './load';

const vec = JSON.parse(
	readFileSync(
		fileURLToPath(new URL('../../backend/tests/vectors/publication_live_v1.json', import.meta.url)),
		'utf-8'
	)
);
const ID: string = vec.pub_id;
const KEY_TEXT: string = vec.key_b64url;
const KEY = b64urlDecode(KEY_TEXT);
const ROOM: string = vec.room;
const ROOM2 = 'ffeeddccbbaa9988';
const SIGN = fromHex(vec.owner.sign_private_hex);
const READER = fromHex(vec.reader.private_hex);
const BASE: Manifest = JSON.parse(vec.manifest.json);
const BEFORE_EXPIRY = new Date('2026-11-05T12:00:00Z');
const AFTER_EXPIRY = new Date('2027-03-01T12:00:00Z');

// ---- sealing a manifest as the owner does (publication_format.py) ------------------------

async function sealManifest(m: Manifest): Promise<{ blob: Uint8Array; sha: string }> {
	const data = new TextEncoder().encode(JSON.stringify(m));
	const size = Math.ceil((4 + data.length) / 4096) * 4096;
	const padded = new Uint8Array(size);
	new DataView(padded.buffer).setUint32(0, data.length, false);
	padded.set(data, 4);
	const nonce = crypto.getRandomValues(new Uint8Array(12));
	const key = await crypto.subtle.importKey('raw', KEY as BufferSource, 'AES-GCM', false, ['encrypt']);
	const aad = new TextEncoder().encode(`mnemosyne/pub/1|${m.id}|manifest|${m.version}`);
	const ct = new Uint8Array(
		await crypto.subtle.encrypt({ name: 'AES-GCM', iv: nonce, additionalData: aad }, key, padded as BufferSource)
	);
	const blob = new Uint8Array(12 + ct.length);
	blob.set(nonce);
	blob.set(ct, 12);
	return { blob, sha: await blobHash(blob) };
}

const version = (v: number, over: Partial<Manifest> = {}): Manifest => ({ ...BASE, version: v, ...over });
const unlive = (v: number): Manifest => {
	const m = version(v);
	delete m.live;
	return m;
};

// ---- the host ---------------------------------------------------------------------------

interface Published {
	blob: Uint8Array;
	sha: string;
	version: number;
}

class FakeHost {
	pub: Published | null = null;
	room: string | null = null;
	/** Overrides the answer to /pub/<id>. */
	status: number | null = null;
	calls: { url: string; init: RequestInit }[] = [];

	async publish(m: Manifest): Promise<Published> {
		const { blob, sha } = await sealManifest(m);
		this.pub = { blob, sha, version: m.version };
		return this.pub;
	}

	fetch = (async (input: RequestInfo | URL, init: RequestInit = {}) => {
		const url = String(input);
		this.calls.push({ url, init });
		if (url !== `/pub/${ID}` || !this.pub) return new Response(null, { status: 404 });
		if (this.status) return new Response(null, { status: this.status });
		const headers: Record<string, string> = {
			ETag: `"${this.pub.sha}"`,
			'Cache-Control': 'no-cache',
			'X-Pub-Version': String(this.pub.version)
		};
		if (this.room) headers['X-Pub-Room'] = this.room;
		const inm = (init.headers as Record<string, string> | undefined)?.['If-None-Match'];
		if (inm && inm.replace(/"/g, '') === this.pub.sha) return new Response(null, { status: 304, headers });
		return new Response(this.pub.blob as BodyInit, { status: 200, headers });
	}) as typeof fetch;

	get manifestCalls() {
		return this.calls.filter((c) => c.url === `/pub/${ID}`);
	}
}

// ---- the room ---------------------------------------------------------------------------

class FakeSocket implements RoomSocket {
	static all: FakeSocket[] = [];
	readyState = 0;
	binaryType = '';
	sent: (string | Uint8Array)[] = [];
	onopen: RoomSocket['onopen'] = null;
	onmessage: RoomSocket['onmessage'] = null;
	onclose: RoomSocket['onclose'] = null;
	onerror: RoomSocket['onerror'] = null;

	constructor(public url: string) {
		FakeSocket.all.push(this);
	}
	send(data: string | Uint8Array): void {
		if (this.readyState !== 1) throw new Error('socket not open');
		this.sent.push(data);
	}
	close(code = 1000): void {
		if (this.readyState >= 2) return;
		this.readyState = 3;
		queueMicrotask(() => this.onclose?.({ code }));
	}
	accept(): void {
		this.readyState = 1;
		this.onopen?.({});
	}
	welcome(seq = 0): void {
		this.onmessage?.({
			data: JSON.stringify({ type: 'welcome', seq, snapshot_seq: null, ended: false, ended_at: null, expires: 99, present: 1 })
		});
	}
	frame(seq: number, env: Uint8Array): void {
		const out = new Uint8Array(9 + env.length);
		out[0] = 0x01;
		new DataView(out.buffer).setBigUint64(1, BigInt(seq), false);
		out.set(env, 9);
		this.onmessage?.({ data: out.buffer });
	}
	serverClose(code: number): void {
		this.readyState = 3;
		this.onclose?.({ code });
	}
	get join(): { type: string; token: string; since: number } {
		return JSON.parse(this.sent[0] as string);
	}
}

const sockets = () => FakeSocket.all;
const lastSocket = () => FakeSocket.all[FakeSocket.all.length - 1];

// ---- harness ----------------------------------------------------------------------------

const immediate = () => new Promise((r) => setImmediate(r));

/** Let WebCrypto (Node's thread pool) and the fetch chain finish: setImmediate and performance are not faked. */
async function settle(ms = 15): Promise<void> {
	const end = performance.now() + ms;
	for (let i = 0; i < 30 || performance.now() < end; i++) await immediate();
}

/** Wait (in real time) until `cond` holds. */
async function until(cond: () => boolean, what: string): Promise<void> {
	const end = performance.now() + 2000;
	while (!cond()) {
		if (performance.now() > end) throw new Error(`timed out waiting for ${what}`);
		await immediate();
	}
}

interface Run {
	host: FakeHost;
	follower: Follower;
	swaps: Shown[];
	gone: Gone[];
	ready: Ready;
}

async function open(host: FakeHost, now = BEFORE_EXPIRY): Promise<Run> {
	const state = await openHosted({ pathname: `/p/${ID}`, hash: `#${KEY_TEXT}` }, host.fetch, now);
	if (state.kind !== 'ready' || !state.hosted) throw new Error(`expected hosted ready, got ${state.kind}`);
	const swaps: Shown[] = [];
	const gone: Gone[] = [];
	const follower = new Follower(
		state.hosted,
		{ manifest: state.manifest, etag: state.etag ?? '', room: state.room ?? null },
		{ onManifest: (s) => swaps.push(s), onGone: (g) => gone.push(g) },
		{ webSocket: FakeSocket, location: { protocol: 'https:', host: 'notes.example' }, now: () => now }
	);
	follower.start();
	await settle();
	return { host, follower, swaps, gone, ready: state };
}

async function tick(ms = POLL_MS): Promise<void> {
	await vi.advanceTimersByTimeAsync(ms);
	await settle();
}

async function joined(room = ROOM): Promise<FakeSocket> {
	await until(() => lastSocket()?.url.endsWith(room) && lastSocket().readyState === 0, `a socket for ${room}`);
	const ws = lastSocket();
	expect(ws.url).toBe(`wss://notes.example/live/${room}`);
	ws.accept();
	ws.welcome();
	await settle();
	return ws;
}

const keys = () => deriveLiveKeys(KEY);
const signed = async (kind: 1 | 2 | 3, obj: object, room = ROOM, key = SIGN) => signFrame(await keys(), room, kind, obj, key);

let follower: Follower | null = null;

beforeEach(() => {
	FakeSocket.all = [];
	vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
	vi.setSystemTime(BEFORE_EXPIRY);
});
afterEach(() => {
	follower?.stop();
	follower = null;
	vi.useRealTimers();
});

// ---- tests ------------------------------------------------------------------------------

describe('the ready state', () => {
	it('carries the ETag and the room header', async () => {
		const host = new FakeHost();
		const pub = await host.publish(version(4));
		host.room = ROOM;
		const state = await openHosted({ pathname: `/p/${ID}`, hash: `#${KEY_TEXT}` }, host.fetch, BEFORE_EXPIRY);
		expect(state.kind === 'ready' && [state.etag, state.room]).toEqual([pub.sha, ROOM]);
		expect(state.kind === 'ready' && state.manifest.live).toEqual(BASE.live);
	});
});

describe('polling', () => {
	it('asks with If-None-Match every minute; a 304 changes nothing', async () => {
		const host = new FakeHost();
		const pub = await host.publish(unlive(4));
		const run = await open(host);
		follower = run.follower;
		expect(host.manifestCalls.length).toBe(1);
		await tick(POLL_MS - 1000);
		expect(host.manifestCalls.length).toBe(1);
		await tick(1000);
		expect(host.manifestCalls.length).toBe(2);
		const call = host.manifestCalls[1];
		expect((call.init.headers as Record<string, string>)['If-None-Match']).toBe(`"${pub.sha}"`);
		expect(call.init.credentials).toBe('omit');
		expect(run.swaps).toEqual([]);
		await tick();
		expect(host.manifestCalls.length).toBe(3);
		expect(run.swaps).toEqual([]);
		expect(run.gone).toEqual([]);
	});

	it('swaps in a newer version, then polls with its ETag', async () => {
		const host = new FakeHost();
		await host.publish(unlive(4));
		const run = await open(host);
		follower = run.follower;
		const next = await host.publish({ ...unlive(5), summary: 'Changed.' });
		await tick();
		expect(run.swaps.length).toBe(1);
		expect(run.swaps[0].manifest.version).toBe(5);
		expect(run.swaps[0].manifest.summary).toBe('Changed.');
		expect(run.swaps[0].etag).toBe(next.sha);
		await tick();
		expect((host.manifestCalls.at(-1)!.init.headers as Record<string, string>)['If-None-Match']).toBe(`"${next.sha}"`);
		expect(run.swaps.length).toBe(1);
	});

	it('maps 404 and 410 to the end of the page, and stops', async () => {
		for (const [status, kind] of [
			[404, 'not_found'],
			[410, 'unpublished']
		] as const) {
			const host = new FakeHost();
			await host.publish(unlive(4));
			const run = await open(host);
			follower = run.follower;
			host.status = status;
			await tick();
			expect(run.gone).toEqual([{ kind }]);
			const n = host.manifestCalls.length;
			await tick();
			expect(host.manifestCalls.length).toBe(n);
			follower.stop();
		}
	});

	it('says "expired" for a 410 once the manifest has expired', async () => {
		const host = new FakeHost();
		await host.publish(unlive(4));
		const run = await open(host);
		follower = run.follower;
		host.status = 410;
		(follower as unknown as { now: () => Date }).now = () => AFTER_EXPIRY;
		await tick();
		expect(run.gone.length).toBe(1);
		expect(run.gone[0].kind).toBe('expired');
	});

	it('skips a failed poll and tries again at the next one', async () => {
		const host = new FakeHost();
		await host.publish(unlive(4));
		const run = await open(host);
		follower = run.follower;
		host.status = 503;
		await tick();
		expect(run.gone).toEqual([]);
		host.status = null;
		await host.publish(unlive(5));
		await tick();
		expect(run.swaps.map((s) => s.manifest.version)).toEqual([5]);
	});

	it('polls only, with no room, when the manifest has no live field or there is no room', async () => {
		const host = new FakeHost();
		await host.publish(unlive(4));
		host.room = ROOM;
		const a = await open(host);
		follower = a.follower;
		expect(sockets()).toEqual([]);
		expect(statusLine(a.follower.following)).toBe(STATUS.polling);
		follower.stop();

		const host2 = new FakeHost();
		await host2.publish(version(4));
		const b = await open(host2);
		follower = b.follower;
		expect(sockets()).toEqual([]);
		await tick();
		expect(host2.manifestCalls.length).toBe(2);
	});
});

describe('the room', () => {
	async function live(): Promise<Run & { ws: FakeSocket }> {
		const host = new FakeHost();
		await host.publish(version(4));
		host.room = ROOM;
		const run = await open(host);
		follower = run.follower;
		const ws = await joined();
		return { ...run, ws };
	}

	it('joins with the token derived from the link key', async () => {
		const { ws } = await live();
		expect(ws.join).toEqual({ type: 'join', token: vec.room_token, since: 0 });
		expect(follower!.room).toBe(ROOM);
		expect(follower!.roomState).toBe('live');
	});

	it('is "kept up to date" only once a signed META says follow and the room is joined', async () => {
		const { ws } = await live();
		expect(follower!.following).toBe(false);
		ws.frame(1, b64urlDecode(vec.samples.meta.envelope_b64url));
		await settle();
		expect(follower!.meta).toEqual(JSON.parse(vec.samples.meta.json));
		expect(follower!.following).toBe(true);
		expect(statusLine(follower!.following)).toBe(STATUS.live);
		ws.serverClose(1006);
		expect(follower!.following).toBe(false);
	});

	it('takes the newest META and THREADS by `at`', async () => {
		const { ws } = await live();
		ws.frame(1, await signed(PubKind.META, { at: '2026-11-05T11:00:00Z', comments: false, follow: false }));
		await settle();
		ws.frame(2, await signed(PubKind.META, { at: '2026-11-05T10:00:00Z', comments: true, follow: true }));
		await settle();
		expect(follower!.meta?.follow).toBe(false);
		ws.frame(3, b64urlDecode(vec.samples.threads.envelope_b64url));
		await settle();
		expect(follower!.threads?.at).toBe('2026-11-05T10:22:31Z');
		ws.frame(4, await signed(PubKind.THREADS, { at: '2026-11-05T09:00:00Z', threads: [] }));
		await settle();
		expect(follower!.threads?.threads.length).toBe(1);
	});

	it('drops frames that are not signed by the owner, silently', async () => {
		const { ws, swaps } = await live();
		ws.frame(1, b64urlDecode(vec.refused.threads_signed_by_reader));
		ws.frame(2, b64urlDecode(vec.refused.version_unsigned));
		ws.frame(3, await signed(PubKind.META, { at: '2026-11-05T11:00:00Z', comments: true, follow: true }, ROOM, READER));
		ws.frame(4, new Uint8Array([1, 2, 3]));
		await settle();
		expect(follower!.dropped).toBe(4);
		expect(follower!.meta).toBeNull();
		expect(follower!.threads).toBeNull();
		expect(swaps).toEqual([]);
	});

	it('refetches on VERSION and swaps when the ETag is the frame sha and the version is higher', async () => {
		const { ws, swaps, host } = await live();
		const next = await host.publish(version(5, { summary: 'Pushed.' }));
		const before = host.manifestCalls.length;
		ws.frame(1, await signed(PubKind.VERSION, { version: 5, sha256: next.sha, published_at: '2026-11-05T12:00:00Z', expires_at: null }));
		await settle();
		expect(host.manifestCalls.length).toBe(before + 1);
		const call = host.manifestCalls.at(-1)!;
		expect(call.init.cache).toBe('no-cache');
		expect(call.init.headers).toBeUndefined();
		expect(swaps.map((s) => [s.manifest.version, s.manifest.summary])).toEqual([[5, 'Pushed.']]);
		// The same room: no new socket.
		expect(sockets().length).toBe(1);
	});

	it('ignores a VERSION whose sha is not the fetched ETag, or that is not newer', async () => {
		const { ws, swaps, host } = await live();
		await host.publish(version(5));
		ws.frame(1, await signed(PubKind.VERSION, { version: 5, sha256: 'ab'.repeat(32), published_at: 'x', expires_at: null }));
		await settle();
		expect(swaps).toEqual([]);
		const n = host.manifestCalls.length;
		ws.frame(2, await signed(PubKind.VERSION, { version: 4, sha256: 'cd'.repeat(32), published_at: 'x', expires_at: null }));
		await settle();
		expect(host.manifestCalls.length).toBe(n);
		expect(follower!.dropped).toBe(0);
	});

	it('re-reads the pointer on 4410 and joins the new room', async () => {
		const { ws, host, gone } = await live();
		host.room = ROOM2;
		const before = host.manifestCalls.length;
		ws.serverClose(4410);
		await settle();
		expect(host.manifestCalls.length).toBe(before + 1);
		expect(gone).toEqual([]);
		const ws2 = await joined(ROOM2);
		expect(ws2.join.token).toBe(vec.room_token);
		expect(follower!.room).toBe(ROOM2);
	});

	it('on 4410 with the same room in the header, polls only; a 410 there is "No longer published"', async () => {
		const { ws, host, gone } = await live();
		ws.serverClose(4410);
		await settle();
		expect(sockets().length).toBe(1);
		expect(follower!.room).toBeNull();
		expect(gone).toEqual([]);
		await tick();
		expect(sockets().length).toBe(1);
		host.status = 410;
		await tick();
		expect(gone).toEqual([{ kind: 'unpublished' }]);
	});

	it('a 4410 followed by a 410 from the publication ends the page', async () => {
		const { ws, host, gone } = await live();
		host.status = 410;
		ws.serverClose(4410);
		await settle();
		expect(gone).toEqual([{ kind: 'unpublished' }]);
	});

	it('on 4401 leaves the room without re-reading and keeps polling', async () => {
		const { ws, host, gone } = await live();
		const before = host.manifestCalls.length;
		ws.serverClose(4401);
		await settle();
		expect(host.manifestCalls.length).toBe(before);
		expect(follower!.room).toBeNull();
		await tick(30_000);
		expect(sockets().length).toBe(1);
		await tick(30_000);
		expect(host.manifestCalls.length).toBe(before + 1);
		expect(gone).toEqual([]);
	});

	it('on 4429 (full or over budget) polls and retries the room with backoff, no error', async () => {
		const { ws, gone } = await live();
		ws.serverClose(4429);
		await settle();
		expect(follower!.roomState).toBe('full');
		expect(follower!.following).toBe(false);
		expect(gone).toEqual([]);
		await tick(1000);
		expect(sockets().length).toBe(2);
		expect(lastSocket().url).toBe(`wss://notes.example/live/${ROOM}`);
	});

	it('joins a room that a later answer names (a 304 included)', async () => {
		const host = new FakeHost();
		await host.publish(version(4));
		const run = await open(host);
		follower = run.follower;
		expect(sockets()).toEqual([]);
		host.room = ROOM;
		await tick();
		expect(run.swaps).toEqual([]);
		await joined();
	});

	it('stop closes the socket and the polls', async () => {
		const { ws, host } = await live();
		follower!.stop();
		await settle();
		expect(ws.readyState).toBe(3);
		const n = host.manifestCalls.length;
		await tick();
		expect(host.manifestCalls.length).toBe(n);
	});
});

describe('publication_live_v1.json manifest', () => {
	it('parseManifest keeps the live keys', () => {
		const m = parseManifest(new TextEncoder().encode(vec.manifest.json));
		expect(m.live).toEqual({ sign: vec.owner.sign_public_hex, box: vec.owner.box_public_hex });
		expect(m.id).toBe(vec.pub_id);
		expect(m.version).toBe(vec.manifest.version);
	});
});
