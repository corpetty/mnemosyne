// @vitest-environment node
// End to end against the committed vectors with real WebCrypto (Node's; jsdom has none).
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it, vi } from 'vitest';
import { b64urlDecode, b64urlEncode, fromHex, hex } from './format';
import {
	expiredOn,
	openFile,
	openHosted,
	parseLocation,
	parsePayload,
	stateMessage,
	unlockFile,
	type FilePayload,
	type ViewState
} from './load';

const vec = JSON.parse(
	readFileSync(
		fileURLToPath(new URL('../../backend/tests/vectors/publication_v1.json', import.meta.url)),
		'utf-8'
	)
);
const ID: string = vec.id;
const KEY: string = vec.key_b64url;
const VERSION: number = vec.manifest.version;
const MANIFEST = b64urlDecode(vec.manifest.blob_b64url);
const CLIP = b64urlDecode(vec.clip.blob_b64url);
const CLIP_SHA: string = vec.clip.sha256;
const BEFORE_EXPIRY = new Date('2026-10-23T12:00:00Z');
const AFTER_EXPIRY = new Date('2027-02-01T12:00:00Z');

type Route = { status: number; body?: Uint8Array; headers?: Record<string, string> };

/** A fetch that answers from a table and records every URL it was asked for. */
function fakeFetch(routes: Record<string, Route>) {
	const urls: string[] = [];
	const fn = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
		const url = String(input);
		urls.push(url);
		const r = routes[url] ?? { status: 404 };
		return new Response(r.body ? (r.body as BodyInit) : null, { status: r.status, headers: r.headers });
	});
	return { fn: fn as unknown as typeof fetch, urls };
}

const host = () =>
	fakeFetch({
		[`/pub/${ID}`]: { status: 200, body: MANIFEST, headers: { 'X-Pub-Version': String(VERSION) } },
		[`/pub/${ID}/${CLIP_SHA}`]: { status: 200, body: CLIP }
	});

const at = (hash: string, path = `/p/${ID}`) => ({ pathname: path, hash });

function ready(state: ViewState) {
	if (state.kind !== 'ready') throw new Error(`expected ready, got ${JSON.stringify(state)}`);
	return state;
}

describe('the address', () => {
	it('takes the id from /p/<id> and the key from the fragment', () => {
		expect(parseLocation(`/p/${ID}`, `#${KEY}`)).toEqual({ id: ID, keyText: KEY });
		expect(parseLocation(`/p/${ID}/`, '')).toEqual({ id: ID, keyText: '' });
		expect(parseLocation('/p/NOTHEX0123456789', '#x').id).toBeNull();
		expect(parseLocation(`/p/${ID}/extra`, '#x').id).toBeNull();
		expect(parseLocation('/', '').id).toBeNull();
	});
});

describe('hosted', () => {
	it('opens the vector publication and plays its clip, never sending the key', async () => {
		const { fn, urls } = host();
		const state = ready(await openHosted(at(`#${KEY}`), fn, BEFORE_EXPIRY));
		expect(state.manifest.title).toBe('Roadmap sync 🚀 – Café Zürich');
		expect(state.manifest.version).toBe(VERSION);
		const clip = await state.loadClip(state.manifest.quotes[0].clip!);
		expect(hex(clip)).toBe(vec.clip.plaintext_hex);
		expect(urls).toEqual([`/pub/${ID}`, `/pub/${ID}/${CLIP_SHA}`]);
		for (const u of urls) {
			expect(u).not.toContain(KEY);
			expect(u).not.toContain('#');
		}
		// Neither in a request option (a header, say).
		for (const call of (fn as unknown as ReturnType<typeof vi.fn>).mock.calls) {
			expect(JSON.stringify(call[1] ?? {})).not.toContain(KEY);
		}
	});

	it('says the link is incomplete without asking the server', async () => {
		const { fn, urls } = host();
		expect(await openHosted(at(''), fn)).toEqual({ kind: 'incomplete', reason: 'missing' });
		expect(await openHosted(at('#'), fn)).toEqual({ kind: 'incomplete', reason: 'missing' });
		expect(await openHosted(at(`#${KEY.slice(0, 40)}`), fn)).toEqual({ kind: 'incomplete', reason: 'malformed' });
		expect(urls).toEqual([]);
	});

	it('maps 404, 410 and other answers', async () => {
		const gone = fakeFetch({ [`/pub/${ID}`]: { status: 410 } });
		expect(await openHosted(at(`#${KEY}`), gone.fn)).toEqual({ kind: 'unpublished' });
		const none = fakeFetch({});
		expect(await openHosted(at(`#${KEY}`), none.fn)).toEqual({ kind: 'not_found' });
		const broken = fakeFetch({ [`/pub/${ID}`]: { status: 503 } });
		expect((await openHosted(at(`#${KEY}`), broken.fn)).kind).toBe('error');
		const offline = vi.fn(async () => {
			throw new TypeError('Failed to fetch');
		}) as unknown as typeof fetch;
		const s = await openHosted(at(`#${KEY}`), offline);
		expect(s.kind === 'error' && s.title).toBe('Could not reach the server');
		// A path that is not a publication id never reaches the server either.
		const { fn, urls } = host();
		expect(await openHosted(at(`#${KEY}`, '/p/'), fn)).toEqual({ kind: 'not_found' });
		expect(urls).toEqual([]);
	});

	it('needs the version header', async () => {
		const noVersion = fakeFetch({ [`/pub/${ID}`]: { status: 200, body: MANIFEST } });
		const s = await openHosted(at(`#${KEY}`), noVersion.fn);
		expect(s.kind === 'error' && s.detail).toMatch(/version/);
	});

	it('shows a decrypt error for a wrong key', async () => {
		const wrong = b64urlDecode(KEY);
		wrong[0] ^= 1;
		const s = await openHosted(at(`#${b64urlEncode(wrong)}`), host().fn, BEFORE_EXPIRY);
		expect(s.kind).toBe('error');
		expect(s.kind === 'error' && s.detail).toMatch(/Could not decrypt/);
	});

	it('shows expiry instead of the meeting once expires_at has passed', async () => {
		const s = await openHosted(at(`#${KEY}`), host().fn, AFTER_EXPIRY);
		expect(s.kind).toBe('expired');
		expect(stateMessage(s)?.title).toMatch(/^This publication expired on \d+ Jan 2027$/);
	});

	it('reports a clip that is gone', async () => {
		const only = fakeFetch({
			[`/pub/${ID}`]: { status: 200, body: MANIFEST, headers: { 'X-Pub-Version': String(VERSION) } }
		});
		const state = ready(await openHosted(at(`#${KEY}`), only.fn, BEFORE_EXPIRY));
		await expect(state.loadClip(state.manifest.quotes[0].clip!)).rejects.toThrow(/no longer available/);
	});
});

describe('single file', () => {
	const keyed: FilePayload = {
		format: 1,
		id: ID,
		version: VERSION,
		manifest: vec.manifest.blob_b64url,
		clips: { [CLIP_SHA]: vec.clip.blob_b64url },
		key: KEY
	};
	const p = vec.password;
	const locked: FilePayload = {
		format: 1,
		id: ID,
		version: VERSION,
		manifest: p.manifest_blob_b64url,
		clips: {},
		password: { salt: b64urlEncode(fromHex(p.salt_hex)), iterations: p.iterations }
	};

	it('parses the payload and refuses damaged ones', () => {
		expect(parsePayload(JSON.stringify(keyed))).toEqual(keyed);
		expect(parsePayload(`\n  ${JSON.stringify(locked)}\n`)).toEqual(locked);
		expect(() => parsePayload('__PAYLOAD__')).toThrow(/no publication/);
		expect(() => parsePayload(null)).toThrow(/no publication/);
		expect(() => parsePayload('{nope')).toThrow(/damaged/);
		expect(() => parsePayload(JSON.stringify({ ...keyed, format: 2 }))).toThrow(/format 2/);
		expect(() => parsePayload(JSON.stringify({ ...keyed, id: 'x' }))).toThrow(/damaged/);
		expect(() => parsePayload(JSON.stringify({ ...keyed, password: locked.password }))).toThrow(/damaged/);
		const { key: _key, ...neither } = keyed;
		expect(() => parsePayload(JSON.stringify(neither))).toThrow(/damaged/);
		expect(() => parsePayload(JSON.stringify({ ...keyed, clips: { abc: 'x' } }))).toThrow(/damaged/);
	});

	it('opens a file with a key, clips included, without fetching', async () => {
		const spy = vi.spyOn(globalThis, 'fetch');
		const state = ready(await openFile(JSON.stringify(keyed), BEFORE_EXPIRY));
		expect(state.manifest.attendees).toContain('李雷');
		expect(hex(await state.loadClip(state.manifest.quotes[0].clip!))).toBe(vec.clip.plaintext_hex);
		expect(spy).not.toHaveBeenCalled();
		spy.mockRestore();
	});

	it('shows the bare template as an error, not a crash', async () => {
		const s = await openFile('__PAYLOAD__');
		expect(s.kind === 'error' && s.detail).toMatch(/no publication/);
	});

	it('asks for the password, refuses a wrong one, opens with the right one', async () => {
		const text = JSON.stringify(locked);
		expect(await openFile(text)).toEqual({ kind: 'password', error: null });
		const wrong = await unlockFile(text, 'not it', BEFORE_EXPIRY);
		expect(wrong).toEqual({ kind: 'password', error: 'That password does not open this file.' });
		const right = ready(await unlockFile(text, p.password, BEFORE_EXPIRY));
		expect(right.manifest.id).toBe(ID);
	});
});

describe('states', () => {
	it('has a message for every state but the meeting and the prompt', () => {
		expect(stateMessage({ kind: 'incomplete', reason: 'missing' })?.title).toBe(
			'This link is incomplete: the part after # is missing'
		);
		expect(stateMessage({ kind: 'not_found' })?.title).toBe("This publication doesn't exist");
		expect(stateMessage({ kind: 'unpublished' })?.title).toBe('No longer published');
		expect(stateMessage({ kind: 'expired', on: '21 Jan 2027' })?.title).toBe(
			'This publication expired on 21 Jan 2027'
		);
		expect(stateMessage({ kind: 'password', error: null })).toBeNull();
	});
	it('treats expiry as inclusive and a null expiry as never', () => {
		const m = { expires_at: '2027-01-21T09:30:00Z' } as Parameters<typeof expiredOn>[0];
		expect(expiredOn(m, new Date('2027-01-21T09:29:59Z'))).toBeNull();
		expect(expiredOn(m, new Date('2027-01-21T09:30:00Z'))).not.toBeNull();
		expect(expiredOn({ ...m, expires_at: null }, AFTER_EXPIRY)).toBeNull();
	});
});
