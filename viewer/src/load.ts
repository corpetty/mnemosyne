// What the viewer shows, decided without any DOM: where the publication comes from (a host or
// the file's own payload), opening it, and the state that results. The key never leaves this
// module except into WebCrypto: request URLs are built from the id and blob hashes only.

import { formatDate } from './display';
import {
	FORMAT,
	MAX_MANIFEST,
	PublicationFormatError,
	b64urlDecode,
	decodeKey,
	deriveKey,
	openClip,
	openManifest,
	type ClipRef,
	type Manifest
} from './format';

/** Hosted: read `/p/<id>#<key>` and fetch from this origin. File: the page's own payload text. */
export type Source = { kind: 'hosted' } | { kind: 'file'; payload: string | null };

/** Fetches and decrypts one clip; resolves to the Ogg Opus bytes. */
export type ClipLoader = (clip: ClipRef) => Promise<Uint8Array>;

export type ViewState =
	| { kind: 'loading' }
	| { kind: 'incomplete'; reason: 'missing' | 'malformed' }
	| { kind: 'not_found' }
	| { kind: 'unpublished' }
	| { kind: 'expired'; on: string }
	| { kind: 'password'; error: string | null }
	| { kind: 'error'; title: string; detail: string }
	| { kind: 'ready'; manifest: Manifest; loadClip: ClipLoader };

/** Heading and explanation for every state that is not the meeting itself. */
export function stateMessage(state: ViewState): { title: string; detail: string } | null {
	switch (state.kind) {
		case 'loading':
			return { title: 'Opening…', detail: '' };
		case 'incomplete':
			return {
				title:
					state.reason === 'missing'
						? 'This link is incomplete: the part after # is missing'
						: 'This link is incomplete: the part after # is cut short or changed',
				detail: 'Ask whoever sent it for the whole link. The part after # is the key that opens the meeting; it never reaches the server.'
			};
		case 'not_found':
			return { title: "This publication doesn't exist", detail: 'Check the link, or ask whoever sent it.' };
		case 'unpublished':
			return { title: 'No longer published', detail: 'Whoever published this meeting has taken it down.' };
		case 'expired':
			return { title: `This publication expired on ${state.on}`, detail: 'Ask whoever sent it for a new link.' };
		case 'error':
			return { title: state.title, detail: state.detail };
		case 'password':
		case 'ready':
			return null;
	}
}

function decryptError(e: unknown): ViewState {
	return {
		kind: 'error',
		title: 'This publication could not be opened',
		detail:
			e instanceof PublicationFormatError
				? e.message
				: 'Your browser could not decrypt it (it needs WebCrypto, which some browsers only offer on https:// pages).'
	};
}

/** Expired when `expires_at` is at or before `now`. */
export function expiredOn(manifest: Manifest, now: Date): string | null {
	if (!manifest.expires_at) return null;
	const at = Date.parse(manifest.expires_at);
	if (Number.isNaN(at) || at > now.getTime()) return null;
	return formatDate(manifest.expires_at);
}

function readyOrExpired(manifest: Manifest, loadClip: ClipLoader, now: Date): ViewState {
	const on = expiredOn(manifest, now);
	return on ? { kind: 'expired', on } : { kind: 'ready', manifest, loadClip };
}

// ---- hosted: /p/<id>#<key> ---------------------------------------------------------------

const ID = /^[0-9a-f]{16}$/;
const SHA = /^[0-9a-f]{64}$/;

/** The id from `/p/<id>` (trailing slash allowed) and the key text after `#`. */
export function parseLocation(pathname: string, hash: string): { id: string | null; keyText: string } {
	const m = /^\/p\/([^/]+)\/?$/.exec(pathname);
	const id = m && ID.test(m[1]) ? m[1] : null;
	return { id, keyText: hash.startsWith('#') ? hash.slice(1) : hash };
}

const REQUEST: RequestInit = { credentials: 'omit', referrerPolicy: 'no-referrer' };

/** Read a publication from the host that served this page: `GET /pub/<id>`, clips on demand. */
export async function openHosted(
	location: { pathname: string; hash: string },
	fetchFn: typeof fetch,
	now: Date = new Date()
): Promise<ViewState> {
	const { id, keyText } = parseLocation(location.pathname, location.hash);
	if (!id) return { kind: 'not_found' };
	if (!keyText) return { kind: 'incomplete', reason: 'missing' };
	let key: Uint8Array;
	try {
		key = decodeKey(keyText);
	} catch {
		return { kind: 'incomplete', reason: 'malformed' };
	}

	let res: Response;
	try {
		res = await fetchFn(`/pub/${id}`, { ...REQUEST, cache: 'no-cache' });
	} catch {
		return { kind: 'error', title: 'Could not reach the server', detail: 'Check your connection and reload the page.' };
	}
	if (res.status === 404) return { kind: 'not_found' };
	if (res.status === 410) return { kind: 'unpublished' };
	if (!res.ok) {
		return { kind: 'error', title: 'The server could not send this publication', detail: `It answered ${res.status}. Try again later.` };
	}
	const version = Number(res.headers.get('x-pub-version'));
	if (!Number.isInteger(version) || version < 1) {
		return { kind: 'error', title: 'This publication could not be opened', detail: 'The server did not say which version it sent.' };
	}
	const blob = new Uint8Array(await res.arrayBuffer());
	if (blob.length > MAX_MANIFEST) {
		return { kind: 'error', title: 'This publication could not be opened', detail: 'It is larger than a publication can be.' };
	}
	let manifest: Manifest;
	try {
		manifest = await openManifest(id, version, blob, key);
	} catch (e) {
		return decryptError(e);
	}
	const loadClip: ClipLoader = async (clip) => {
		if (!SHA.test(clip.sha256)) throw new PublicationFormatError('The clip has a malformed address');
		const r = await fetchFn(`/pub/${id}/${clip.sha256}`, REQUEST);
		if (r.status === 404 || r.status === 410) throw new Error('This clip is no longer available');
		if (!r.ok) throw new Error(`The server answered ${r.status}`);
		return openClip(id, clip, new Uint8Array(await r.arrayBuffer()), key);
	};
	return readyOrExpired(manifest, loadClip, now);
}

// ---- a single file: the payload inlined in the page --------------------------------------

/**
 * The JSON that the file export puts in `<script type="application/json" id="payload">`.
 * All binary values are unpadded base64url. Exactly one of `key` and `password`.
 */
export interface FilePayload {
	format: 1;
	/** The publication id (16 hex), part of every blob's AAD. */
	id: string;
	/** The manifest's version, part of its AAD. */
	version: number;
	/** The sealed manifest blob. */
	manifest: string;
	/** Sealed clip blobs by their address (lowercase hex SHA-256 of the blob). */
	clips: Record<string, string>;
	/** The 32-byte key (43 characters), for a file without a password. */
	key?: string;
	/** For a file with a password: PBKDF2-HMAC-SHA256 over the password with this salt (16 bytes). */
	password?: { salt: string; iterations: number };
}

/** The payload from the page. Throws PublicationFormatError when the slot was never filled or is damaged. */
export function parsePayload(text: string | null | undefined): FilePayload {
	const trimmed = (text ?? '').trim();
	if (!trimmed.startsWith('{')) throw new PublicationFormatError('This file has no publication in it');
	let raw: unknown;
	try {
		raw = JSON.parse(trimmed);
	} catch {
		throw new PublicationFormatError('The publication in this file is damaged');
	}
	const bad = () => new PublicationFormatError('The publication in this file is damaged');
	if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) throw bad();
	const p = raw as Record<string, unknown>;
	if (p.format !== FORMAT) {
		throw new PublicationFormatError(
			`This file uses format ${JSON.stringify(p.format)}; this viewer reads format ${FORMAT}`
		);
	}
	if (typeof p.id !== 'string' || !ID.test(p.id)) throw bad();
	if (typeof p.version !== 'number' || !Number.isInteger(p.version) || p.version < 1) throw bad();
	if (typeof p.manifest !== 'string') throw bad();
	const clips = p.clips ?? {};
	if (typeof clips !== 'object' || clips === null || Array.isArray(clips)) throw bad();
	for (const [sha, blob] of Object.entries(clips)) {
		if (!SHA.test(sha) || typeof blob !== 'string') throw bad();
	}
	const hasKey = typeof p.key === 'string';
	const pw = p.password as Record<string, unknown> | undefined;
	const hasPassword =
		typeof pw === 'object' && pw !== null && typeof pw.salt === 'string' && typeof pw.iterations === 'number';
	if (hasKey === hasPassword) throw bad();
	return {
		format: FORMAT,
		id: p.id,
		version: p.version,
		manifest: p.manifest,
		clips: clips as Record<string, string>,
		...(hasKey ? { key: p.key as string } : { password: { salt: pw!.salt as string, iterations: pw!.iterations as number } })
	};
}

/** Read the payload element's text; any problem with it becomes an error state. */
export async function openFile(text: string | null | undefined, now: Date = new Date()): Promise<ViewState> {
	let payload: FilePayload;
	try {
		payload = parsePayload(text);
	} catch (e) {
		return { kind: 'error', title: 'This file could not be opened', detail: (e as Error).message };
	}
	if (payload.password) return { kind: 'password', error: null };
	let key: Uint8Array;
	try {
		key = decodeKey(payload.key!);
	} catch {
		return { kind: 'error', title: 'This file could not be opened', detail: 'Its key is damaged.' };
	}
	return openWithKey(payload, key, now);
}

/** Try a password on a password-protected file. A wrong one gives the prompt back with an error. */
export async function unlockFile(text: string | null | undefined, password: string, now: Date = new Date()): Promise<ViewState> {
	let payload: FilePayload;
	try {
		payload = parsePayload(text);
	} catch (e) {
		return { kind: 'error', title: 'This file could not be opened', detail: (e as Error).message };
	}
	if (!payload.password) return openFile(text, now);
	let key: Uint8Array;
	try {
		key = await deriveKey(password, b64urlDecode(payload.password.salt), payload.password.iterations);
	} catch (e) {
		return decryptError(e);
	}
	const state = await openWithKey(payload, key, now);
	if (state.kind === 'error' && state.title === 'This publication could not be opened') {
		return { kind: 'password', error: 'That password does not open this file.' };
	}
	return state;
}

async function openWithKey(payload: FilePayload, key: Uint8Array, now: Date): Promise<ViewState> {
	let manifest: Manifest;
	try {
		manifest = await openManifest(payload.id, payload.version, b64urlDecode(payload.manifest), key);
	} catch (e) {
		return decryptError(e);
	}
	const loadClip: ClipLoader = async (clip) => {
		const blob = payload.clips[clip.sha256];
		if (blob === undefined) throw new Error('This clip is not in the file');
		return openClip(payload.id, clip, b64urlDecode(blob), key);
	};
	return readyOrExpired(manifest, loadClip, now);
}
