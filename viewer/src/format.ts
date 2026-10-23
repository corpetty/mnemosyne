// The publication bundle format, reading half. The writing half is
// backend/mnemosyne/services/publication_format.py (its docstring has the rules); the two are
// tied together by backend/tests/vectors/publication_v1.json. Browser only needs to decode, so
// nothing here encrypts. Uses WebCrypto (`crypto.subtle`), which works from file:// too.

export const FORMAT = 1;
export const AAD_PREFIX = 'mnemosyne/pub/1';
export const NONCE_SIZE = 12;
export const TAG_SIZE = 16;
export const KEY_SIZE = 32;
export const KEY_B64_LEN = 43;
export const SALT_SIZE = 16;

export const MANIFEST_BLOCK = 4096;
export const CLIP_BLOCK = 65536;
export const MAX_MANIFEST = 1024 * 1024;
export const MAX_CLIP = 20 * 1024 * 1024;
export const MAX_CLIPS_TOTAL = 20 * 1024 * 1024;

export const PBKDF2_ITERATIONS = 600_000;
export const PBKDF2_MIN_ITERATIONS = 100_000;
export const PBKDF2_MAX_ITERATIONS = 10_000_000;

export type Kind = 'manifest' | 'clip';

// ---- manifest types (mirror Manifest in publication_format.py; no transcript field) ------

export interface ClipRef {
  sha256: string;
  size: number;
  seconds: number;
  mime: string;
  /** The version that first uploaded this clip; part of its AAD. */
  v: number;
}
export interface Decision {
  id: string;
  text: string;
  at: number | null;
}
export interface Question {
  id: string;
  text: string;
  at: number | null;
}
export interface ActionItem {
  id: string;
  text: string;
  owner: string | null;
  done: boolean;
  due: string | null;
}
export interface Chapter {
  title: string;
  start: number;
}
export interface Quote {
  id: string;
  text: string;
  speaker: string | null;
  start: number;
  end: number;
  clip: ClipRef | null;
}
export interface Manifest {
  format: 1;
  id: string;
  version: number;
  published_at: string;
  expires_at: string | null;
  title: string;
  date: string;
  duration_seconds: number | null;
  summary: string;
  decisions: Decision[];
  questions: Question[];
  action_items: ActionItem[];
  chapters: Chapter[];
  attendees: string[];
  topics: string[];
  notes: string | null;
  quotes: Quote[];
}

export class PublicationFormatError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'PublicationFormatError';
  }
}

// ---- base64url, hex ----------------------------------------------------------------------

const B64URL = /^[A-Za-z0-9_-]+$/;

export function b64urlEncode(data: Uint8Array): string {
  let s = '';
  for (const b of data) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function b64urlDecode(text: string): Uint8Array {
  if (!B64URL.test(text) || text.length % 4 === 1) {
    throw new PublicationFormatError('Not valid base64url');
  }
  const std = text.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (text.length % 4)) % 4);
  const bin = atob(std);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  if (b64urlEncode(out) !== text) throw new PublicationFormatError('Not valid base64url');
  return out;
}

export function hex(data: Uint8Array): string {
  return Array.from(data, (b) => b.toString(16).padStart(2, '0')).join('');
}

export function fromHex(text: string): Uint8Array {
  if (!/^([0-9a-f]{2})*$/i.test(text)) throw new PublicationFormatError('Not valid hex');
  const out = new Uint8Array(text.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(text.slice(i * 2, i * 2 + 2), 16);
  return out;
}

/** The 32-byte key from the link fragment (43 characters of unpadded base64url). */
export function decodeKey(text: string): Uint8Array {
  if (text.length !== KEY_B64_LEN) {
    throw new PublicationFormatError("That link's key is not the right length");
  }
  const key = b64urlDecode(text);
  if (key.length !== KEY_SIZE) throw new PublicationFormatError("That link's key is not the right length");
  return key;
}

// ---- padding, AAD, addresses -------------------------------------------------------------

export function unpad(padded: Uint8Array, block: number): Uint8Array {
  if (padded.length < 4 || padded.length % block !== 0) {
    throw new PublicationFormatError('The padded data has the wrong size');
  }
  const length = new DataView(padded.buffer, padded.byteOffset, 4).getUint32(0, false);
  if (4 + length > padded.length || padded.length - (4 + length) >= block) {
    throw new PublicationFormatError('The padded data has a bad length');
  }
  for (let i = 4 + length; i < padded.length; i++) {
    if (padded[i] !== 0) throw new PublicationFormatError('The padding is not zeros');
  }
  return padded.slice(4, 4 + length);
}

export function aad(id: string, kind: Kind, version: number): Uint8Array {
  if (kind !== 'manifest' && kind !== 'clip') {
    throw new PublicationFormatError(`Unknown blob kind ${String(kind)}`);
  }
  return new TextEncoder().encode(`${AAD_PREFIX}|${id}|${kind}|${version}`);
}

/** Lowercase hex SHA-256 of a whole blob: its address on a host. */
export async function blobHash(blob: Uint8Array): Promise<string> {
  return hex(new Uint8Array(await crypto.subtle.digest('SHA-256', blob as BufferSource)));
}

// ---- opening -----------------------------------------------------------------------------

/** Decrypt and unpad. Throws PublicationFormatError on a wrong key, id, kind or version, or an altered blob. */
export async function openBlob(
  kind: Kind,
  id: string,
  version: number,
  blob: Uint8Array,
  key: Uint8Array
): Promise<Uint8Array> {
  const additional = aad(id, kind, version);
  if (key.length !== KEY_SIZE) throw new PublicationFormatError('A publication key is 32 bytes');
  if (blob.length < NONCE_SIZE + TAG_SIZE) {
    throw new PublicationFormatError('The data is too short to be a publication blob');
  }
  const cryptoKey = await crypto.subtle.importKey('raw', key as BufferSource, 'AES-GCM', false, [
    'decrypt'
  ]);
  let padded: Uint8Array;
  try {
    padded = new Uint8Array(
      await crypto.subtle.decrypt(
        { name: 'AES-GCM', iv: blob.slice(0, NONCE_SIZE), additionalData: additional as BufferSource },
        cryptoKey,
        blob.slice(NONCE_SIZE)
      )
    );
  } catch {
    throw new PublicationFormatError(
      `Could not decrypt the ${kind}: wrong key, or it was altered, or it belongs to another publication or version`
    );
  }
  return unpad(padded, kind === 'manifest' ? MANIFEST_BLOCK : CLIP_BLOCK);
}

export function parseManifest(data: Uint8Array): Manifest {
  let raw: unknown;
  try {
    raw = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(data));
  } catch {
    throw new PublicationFormatError('The manifest is not valid JSON');
  }
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    throw new PublicationFormatError('The manifest is malformed');
  }
  const m = raw as Record<string, unknown>;
  if (m.format !== FORMAT) {
    throw new PublicationFormatError(
      `This publication uses format ${JSON.stringify(m.format)}; this viewer reads format ${FORMAT}`
    );
  }
  if (typeof m.id !== 'string' || typeof m.version !== 'number' || typeof m.title !== 'string') {
    throw new PublicationFormatError('The manifest is malformed');
  }
  return m as unknown as Manifest;
}

export async function openManifest(
  id: string,
  version: number,
  blob: Uint8Array,
  key: Uint8Array
): Promise<Manifest> {
  const manifest = parseManifest(await openBlob('manifest', id, version, blob, key));
  if (manifest.id !== id || manifest.version !== version) {
    throw new PublicationFormatError('The manifest is for another publication or version');
  }
  return manifest;
}

/** Open a clip blob using the version recorded in its manifest entry (`clip.v`). */
export async function openClip(
  id: string,
  clip: ClipRef,
  blob: Uint8Array,
  key: Uint8Array
): Promise<Uint8Array> {
  if ((await blobHash(blob)) !== clip.sha256) {
    throw new PublicationFormatError('The clip does not match its address');
  }
  return openBlob('clip', id, clip.v, blob, key);
}

// ---- a key from a password ---------------------------------------------------------------

export async function deriveKey(
  password: string,
  salt: Uint8Array,
  iterations: number = PBKDF2_ITERATIONS
): Promise<Uint8Array> {
  if (!(iterations >= PBKDF2_MIN_ITERATIONS && iterations <= PBKDF2_MAX_ITERATIONS)) {
    throw new PublicationFormatError('The password iteration count is out of range');
  }
  if (salt.length !== SALT_SIZE) throw new PublicationFormatError('The password salt is 16 bytes');
  const base = await crypto.subtle.importKey(
    'raw',
    new TextEncoder().encode(password) as BufferSource,
    'PBKDF2',
    false,
    ['deriveBits']
  );
  const bits = await crypto.subtle.deriveBits(
    { name: 'PBKDF2', hash: 'SHA-256', salt: salt as BufferSource, iterations },
    base,
    KEY_SIZE * 8
  );
  return new Uint8Array(bits);
}
