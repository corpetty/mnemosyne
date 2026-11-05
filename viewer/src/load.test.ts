// @vitest-environment node
// End to end against the committed vectors with real WebCrypto (Node's; jsdom has none).
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { b64urlDecode, b64urlEncode, fromHex, hex } from './format';
import {
	createImageLoader,
	expiredOn,
	openFile,
	openHosted,
	parseEtag,
	parseLocation,
	parsePayload,
	parseRoom,
	stateMessage,
	swap,
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
const IM = vec.image;
const IMAGES_MANIFEST = b64urlDecode(vec.images_manifest.blob_b64url);
const IMAGES_VERSION: number = vec.images_manifest.version;
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

describe('following a hosted publication', () => {
	it('keeps the ETag and the room header with the ready state', async () => {
		const tagged = fakeFetch({
			[`/pub/${ID}`]: {
				status: 200,
				body: MANIFEST,
				headers: { 'X-Pub-Version': String(VERSION), ETag: `"${'a'.repeat(64)}"`, 'X-Pub-Room': '0011223344556677' }
			}
		});
		const s = ready(await openHosted(at(`#${KEY}`), tagged.fn, BEFORE_EXPIRY));
		expect([s.etag, s.room, s.hosted?.id]).toEqual(['a'.repeat(64), '0011223344556677', ID]);
		const plain = ready(await openHosted(at(`#${KEY}`), host().fn, BEFORE_EXPIRY));
		expect([plain.etag, plain.room]).toEqual(['', null]);
	});

	it('reads ETag and room headers strictly', () => {
		expect(parseEtag('"abc"')).toBe('abc');
		expect(parseEtag('W/"abc"')).toBe('abc');
		expect(parseEtag(null)).toBe('');
		expect(parseRoom('0011223344556677')).toBe('0011223344556677');
		expect(parseRoom('0011')).toBeNull();
		expect(parseRoom('../../x')).toBeNull();
		expect(parseRoom(null)).toBeNull();
	});

	it('maps 304, 404 and 410 on a later fetch', async () => {
		// The handle from a fake that answers by the If-None-Match it is given.
		const hosted = ready(
			await openHosted(
				at(`#${KEY}`),
				(async (_u: RequestInfo | URL, init?: RequestInit) => {
					const inm = (init?.headers as Record<string, string> | undefined)?.['If-None-Match'];
					if (inm === '"e"') return new Response(null, { status: 304, headers: { 'X-Pub-Room': '0011223344556677' } });
					if (inm === '"gone"') return new Response(null, { status: 410 });
					if (inm === '"none"') return new Response(null, { status: 404 });
					return new Response(MANIFEST as BodyInit, { status: 200, headers: { 'X-Pub-Version': String(VERSION) } });
				}) as typeof fetch,
				BEFORE_EXPIRY
			)
		).hosted!;
		expect(await hosted.fetch({ etag: 'e', cache: 'no-store' })).toEqual({ kind: 'not_modified', etag: 'e', room: '0011223344556677' });
		expect(await hosted.fetch({ etag: 'gone', cache: 'no-store' })).toEqual({ kind: 'unpublished' });
		expect(await hosted.fetch({ etag: 'none', cache: 'no-store' })).toEqual({ kind: 'not_found' });
		const m = await hosted.fetch({ cache: 'no-cache' });
		expect(m.kind === 'manifest' && m.manifest.version).toBe(VERSION);
	});

	it('swaps a manifest in, keeping the loaders, or gives the expiry', async () => {
		const s = ready(await openHosted(at(`#${KEY}`), host().fn, BEFORE_EXPIRY));
		const next = { manifest: { ...s.manifest, version: VERSION + 1 }, etag: 'b'.repeat(64), room: null };
		const swapped = ready(swap(s, next, BEFORE_EXPIRY));
		expect(swapped.manifest.version).toBe(VERSION + 1);
		expect(swapped.loadClip).toBe(s.loadClip);
		expect(swapped.loadImage).toBe(s.loadImage);
		expect(swapped.hosted).toBe(s.hosted);
		expect(swapped.etag).toBe('b'.repeat(64));
		expect(swap(s, next, AFTER_EXPIRY).kind).toBe('expired');
	});
});

describe('images', () => {
	const hostWithImage = () =>
		fakeFetch({
			[`/pub/${ID}`]: { status: 200, body: IMAGES_MANIFEST, headers: { 'X-Pub-Version': String(IMAGES_VERSION) } },
			[`/pub/${ID}/${IM.sha256}`]: { status: 200, body: b64urlDecode(IM.blob_b64url) }
		});

	beforeEach(() => {
		URL.createObjectURL = vi.fn(() => 'blob:image-1');
		URL.revokeObjectURL = vi.fn();
	});

	it('fetches an image by its blob address, once, and decrypts it', async () => {
		const { fn, urls } = hostWithImage();
		const state = ready(await openHosted(at(`#${KEY}`), fn, BEFORE_EXPIRY));
		const ref = state.manifest.images![0];
		expect(urls).toEqual([`/pub/${ID}`]); // nothing yet: images load when shown
		expect(await state.loadImage(ref)).toBe('blob:image-1');
		expect(await state.loadImage(ref)).toBe('blob:image-1');
		expect(urls).toEqual([`/pub/${ID}`, `/pub/${ID}/${IM.sha256}`]);
		expect(urls.join()).not.toContain(KEY);
		const blob = vi.mocked(URL.createObjectURL).mock.calls[0][0] as Blob;
		expect(blob.type).toBe('image/webp');
		expect(hex(new Uint8Array(await blob.arrayBuffer()))).toBe(IM.plaintext_hex);
		state.loadImage.dispose!();
		await vi.waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:image-1'));
	});

	it('reports an image that is gone, and tries again next time', async () => {
		const only = fakeFetch({
			[`/pub/${ID}`]: { status: 200, body: IMAGES_MANIFEST, headers: { 'X-Pub-Version': String(IMAGES_VERSION) } }
		});
		const state = ready(await openHosted(at(`#${KEY}`), only.fn, BEFORE_EXPIRY));
		const ref = state.manifest.images![0];
		await expect(state.loadImage(ref)).rejects.toThrow(/no longer available/);
		await expect(state.loadImage(ref)).rejects.toThrow(/no longer available/);
		expect(only.urls.filter((u) => u.endsWith(IM.sha256)).length).toBe(2);
	});

	it('refuses a malformed address or a type that is not WebP or JPEG', async () => {
		const { fn, urls } = hostWithImage();
		const state = ready(await openHosted(at(`#${KEY}`), fn, BEFORE_EXPIRY));
		const ref = state.manifest.images![0];
		await expect(state.loadImage({ ...ref, id: 'a'.repeat(64), sha256: '../x' })).rejects.toThrow(/malformed/);
		await expect(state.loadImage({ ...ref, id: 'b'.repeat(64), mime: 'image/svg+xml' })).rejects.toThrow(/unknown type/);
		expect(urls).toEqual([`/pub/${ID}`]);
	});

	it('reads images from the file itself, without fetching', async () => {
		const spy = vi.spyOn(globalThis, 'fetch');
		const payload: FilePayload = {
			format: 1,
			id: ID,
			version: IMAGES_VERSION,
			manifest: vec.images_manifest.blob_b64url,
			clips: {},
			images: { [IM.sha256]: IM.blob_b64url },
			key: KEY
		};
		expect(parsePayload(JSON.stringify(payload))).toEqual(payload);
		const state = ready(await openFile(JSON.stringify(payload), BEFORE_EXPIRY));
		expect(await state.loadImage(state.manifest.images![0])).toBe('blob:image-1');
		expect(spy).not.toHaveBeenCalled();
		spy.mockRestore();
		// A file without the blob says so; a damaged images table is a damaged file.
		const bare = ready(await openFile(JSON.stringify({ ...payload, images: undefined }), BEFORE_EXPIRY));
		await expect(bare.loadImage(bare.manifest.images![0])).rejects.toThrow(/not in the file/);
		expect(() => parsePayload(JSON.stringify({ ...payload, images: { abc: 'x' } }))).toThrow(/damaged/);
		expect(() => parsePayload(JSON.stringify({ ...payload, images: [] }))).toThrow(/damaged/);
	});

	it('makes one URL per image id, even for concurrent asks', async () => {
		const open = vi.fn(async () => new Uint8Array([1]));
		const load = createImageLoader(open);
		const ref = { id: 'a'.repeat(64), sha256: 'b'.repeat(64), size: 1, mime: 'image/jpeg', v: 1, width: 1, height: 1 };
		await Promise.all([load(ref), load(ref)]);
		expect(open).toHaveBeenCalledTimes(1);
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
