// Sealed room envelopes, browser half, for every namespace (`Space`) that shares the shape:
// guest notes (guest/src/format.ts, GUEST) and live publications (signed.ts, PUB_LIVE). The Python
// half is backend/mnemosyne/services/guest_format.py (`Space`, `GUEST`) and pub_live_format.py;
// backend/tests/vectors/guest_v1.json and publication_live_v1.json tie them together. Change one,
// change the other and the vectors. WebCrypto only; imports nothing from the app (the guest and
// viewer entries bundle it).
//
// Rules, per space:
// - a room id is 16 lowercase hex characters; the key is 32 random bytes (the link's).
// - framesKey = HKDF-SHA256(key, salt "", info space.infoFrames, 32 bytes); roomToken =
//   b64url(HKDF-SHA256(key, salt "", info space.infoRoom, 32 bytes)); with space.infoMediaName,
//   mediaNameKey = HKDF-SHA256(key, salt "", info space.infoMediaName, 32 bytes).
// - envelope = kind(1) || nonce(12) || AES-256-GCM(framesKey, nonce, padded, AAD) with padded =
//   u32 BE length || plaintext || zeros up to a multiple of the kind's block (space.blocks, else
//   BLOCK = 256); the ciphertext ends in the 16-byte tag.
// - AAD = UTF-8 of `<space.aadPrefix>|<room>|<kind as decimal>`; for space.mediaKind one more field
//   `|<sha>` (the plaintext's SHA-256, 64 lowercase hex), required there and refused elsewhere.

export const NONCE_SIZE = 12;
export const TAG_SIZE = 16;
export const KEY_SIZE = 32;
export const ROOM_HEX_LEN = 16;
export const BLOCK = 256;

/** Raised when data cannot be sealed or opened; the message says why. */
export class SealedFormatError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'SealedFormatError';
  }
}

export type ErrorCtor = new (message: string) => Error;

/** One namespace of sealed room frames. */
export interface Space {
  /** What messages call its keys and frames ("guest": "A guest key is 32 bytes"). */
  label: string;
  /** `mnemosyne/guest/1`: the AAD's first field. */
  aadPrefix: string;
  infoFrames: string;
  infoRoom: string;
  infoMediaName?: string;
  /** The kind bytes that exist. */
  kinds: readonly number[];
  /** Kinds whose padding block is not BLOCK. */
  blocks: Readonly<Record<number, number>>;
  /** Kinds whose plaintext is canonical JSON (sealJson/unsealJson). */
  jsonKinds: readonly number[];
  /** The kind whose AAD carries the plaintext's sha. */
  mediaKind?: number;
  /** Thrown for this space's errors (a subclass of SealedFormatError); default SealedFormatError. */
  error?: ErrorCtor;
}

/** The guest-notes space (phase 9): exactly guest_format.py's rules. */
export const GUEST: Space = {
  label: 'guest',
  aadPrefix: 'mnemosyne/guest/1',
  infoFrames: 'mnemosyne/guest/frames',
  infoRoom: 'mnemosyne/guest/room',
  infoMediaName: 'mnemosyne/guest/media-name',
  kinds: [1, 2, 3, 4, 5, 6],
  blocks: { 2: 4096, 6: 65536 },
  jsonKinds: [2, 3, 4, 5],
  mediaKind: 6
};

export interface SpaceKeys {
  framesKey: CryptoKey;
  /** Raw bytes of the frames key (for tests). */
  framesKeyBytes: Uint8Array;
  roomToken: string;
  /** HMAC-SHA256 key for media names (only in a space with infoMediaName). */
  mediaNameKey?: CryptoKey;
  mediaNameKeyBytes?: Uint8Array;
}

const enc = new TextEncoder();
const dec = new TextDecoder('utf-8', { fatal: true });
export const buf = (u: Uint8Array) => u as BufferSource;

function fail(err: ErrorCtor | undefined, message: string): never {
  throw new (err ?? SealedFormatError)(message);
}

// ---- encodings -----------------------------------------------------------------------

const B64URL = /^[A-Za-z0-9_-]*$/;

export function b64urlEncode(data: Uint8Array): string {
  let bin = '';
  for (const b of data) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** Strict: refuses other characters, impossible lengths and non-zero trailing bits. */
export function b64urlDecode(text: string, err?: ErrorCtor): Uint8Array {
  if (!B64URL.test(text) || text.length % 4 === 1) fail(err, 'Not valid base64url');
  const std = text.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (text.length % 4)) % 4);
  const bin = atob(std);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  if (b64urlEncode(out) !== text) fail(err, 'Not valid base64url');
  return out;
}

export function hex(data: Uint8Array): string {
  return Array.from(data, (b) => b.toString(16).padStart(2, '0')).join('');
}

export function fromHex(text: string, err?: ErrorCtor): Uint8Array {
  if (!/^([0-9a-f]{2})*$/i.test(text)) fail(err, 'Not valid hex');
  const out = new Uint8Array(text.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(text.slice(i * 2, i * 2 + 2), 16);
  return out;
}

export function concat(...parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let pos = 0;
  for (const p of parts) {
    out.set(p, pos);
    pos += p.length;
  }
  return out;
}

// ---- ids and keys --------------------------------------------------------------------

export function newRoom(): string {
  return hex(crypto.getRandomValues(new Uint8Array(ROOM_HEX_LEN / 2)));
}

export function newKey(): Uint8Array {
  return crypto.getRandomValues(new Uint8Array(KEY_SIZE));
}

export function checkRoom(room: string, err?: ErrorCtor): string {
  if (typeof room !== 'string' || !/^[0-9a-f]{16}$/.test(room)) {
    fail(err, 'A room id is 16 lowercase hex characters');
  }
  return room;
}

export function checkKey(space: Space, key: Uint8Array): void {
  if (!(key instanceof Uint8Array) || key.length !== KEY_SIZE) {
    fail(space.error, `A ${space.label} key is 32 bytes`);
  }
}

/** HKDF-SHA256 of `ikm` (32 bytes out unless `bits` says otherwise). */
export async function hkdf(
  ikm: Uint8Array,
  info: string | Uint8Array,
  salt: Uint8Array = new Uint8Array(0),
  bits = 256
): Promise<Uint8Array> {
  const base = await crypto.subtle.importKey('raw', buf(ikm), 'HKDF', false, ['deriveBits']);
  const out = await crypto.subtle.deriveBits(
    {
      name: 'HKDF',
      hash: 'SHA-256',
      salt: buf(salt),
      info: buf(typeof info === 'string' ? enc.encode(info) : info)
    },
    base,
    bits
  );
  return new Uint8Array(out);
}

export async function aesKey(raw: Uint8Array): Promise<CryptoKey> {
  return crypto.subtle.importKey('raw', buf(raw), 'AES-GCM', false, ['encrypt', 'decrypt']);
}

/** The frames key, room token and (where the space has one) media-name key of a link key. */
export async function deriveSpaceKeys(space: Space, key: Uint8Array): Promise<SpaceKeys> {
  checkKey(space, key);
  const framesKeyBytes = await hkdf(key, space.infoFrames);
  const keys: SpaceKeys = {
    framesKey: await aesKey(framesKeyBytes),
    framesKeyBytes,
    roomToken: b64urlEncode(await hkdf(key, space.infoRoom))
  };
  if (space.infoMediaName !== undefined) {
    keys.mediaNameKeyBytes = await hkdf(key, space.infoMediaName);
    keys.mediaNameKey = await crypto.subtle.importKey(
      'raw',
      buf(keys.mediaNameKeyBytes),
      { name: 'HMAC', hash: 'SHA-256' },
      false,
      ['sign']
    );
  }
  return keys;
}

const SHA_RE = /^[0-9a-f]{64}$/;

export function checkSha(sha: string, err?: ErrorCtor): string {
  if (typeof sha !== 'string' || !SHA_RE.test(sha)) {
    fail(err, 'A media sha is 64 lowercase hex characters');
  }
  return sha;
}

export async function sha256(data: Uint8Array): Promise<Uint8Array> {
  return new Uint8Array(await crypto.subtle.digest('SHA-256', buf(data)));
}

export async function sha256Hex(data: Uint8Array): Promise<string> {
  return hex(await sha256(data));
}

// ---- padding, AAD --------------------------------------------------------------------

export function checkKind(space: Space, kind: number): number {
  if (!Number.isInteger(kind) || !space.kinds.includes(kind)) {
    fail(space.error, `Unknown frame kind ${String(kind)}`);
  }
  return kind;
}

export function blockFor(space: Space, kind: number): number {
  return space.blocks[checkKind(space, kind)] ?? BLOCK;
}

/** `sha` (the plaintext's SHA-256, hex) is required for the media kind and refused for others. */
export function aad(space: Space, room: string, kind: number, sha?: string): Uint8Array {
  const k = checkKind(space, kind);
  const base = `${space.aadPrefix}|${checkRoom(room, space.error)}|${k}`;
  if (space.mediaKind !== undefined && k === space.mediaKind) {
    if (sha === undefined) fail(space.error, 'A media frame needs its sha');
    return enc.encode(`${base}|${checkSha(sha, space.error)}`);
  }
  if (sha !== undefined) fail(space.error, 'Only a media frame has a sha');
  return enc.encode(base);
}

export function pad(data: Uint8Array, block: number): Uint8Array {
  const total = Math.ceil((4 + data.length) / block) * block;
  const out = new Uint8Array(total);
  new DataView(out.buffer).setUint32(0, data.length, false);
  out.set(data, 4);
  return out;
}

export function unpad(padded: Uint8Array, block: number, err?: ErrorCtor): Uint8Array {
  if (padded.length < 4 || padded.length % block !== 0) {
    fail(err, 'The padded data has the wrong size');
  }
  const length = new DataView(padded.buffer, padded.byteOffset, 4).getUint32(0, false);
  if (4 + length > padded.length || padded.length - (4 + length) >= block) {
    fail(err, 'The padded data has a bad length');
  }
  for (let i = 4 + length; i < padded.length; i++) {
    if (padded[i] !== 0) fail(err, 'The padding is not zeros');
  }
  return padded.slice(4, 4 + length);
}

// ---- sealing -------------------------------------------------------------------------

/** Pad and encrypt into an envelope. `nonce` is for the test vectors only. */
export async function seal(
  space: Space,
  keys: { framesKey: CryptoKey },
  room: string,
  kind: number,
  plaintext: Uint8Array,
  nonce?: Uint8Array,
  sha?: string
): Promise<Uint8Array> {
  const k = checkKind(space, kind);
  const additional = aad(space, room, k, sha);
  const padded = pad(plaintext, blockFor(space, k));
  if (nonce === undefined) nonce = crypto.getRandomValues(new Uint8Array(NONCE_SIZE));
  else if (nonce.length !== NONCE_SIZE) fail(space.error, 'A nonce is 12 bytes');
  const sealed = new Uint8Array(
    await crypto.subtle.encrypt(
      { name: 'AES-GCM', iv: buf(nonce), additionalData: buf(additional), tagLength: 128 },
      keys.framesKey,
      buf(padded)
    )
  );
  const out = new Uint8Array(1 + NONCE_SIZE + sealed.length);
  out[0] = k;
  out.set(nonce, 1);
  out.set(sealed, 1 + NONCE_SIZE);
  return out;
}

export async function unseal(
  space: Space,
  keys: { framesKey: CryptoKey },
  room: string,
  envelope: Uint8Array,
  sha?: string
): Promise<{ kind: number; plaintext: Uint8Array }> {
  checkRoom(room, space.error);
  if (envelope.length < 1 + NONCE_SIZE + TAG_SIZE) {
    fail(space.error, `The data is too short to be a ${space.label} frame`);
  }
  const kind = checkKind(space, envelope[0]);
  const nonce = envelope.slice(1, 1 + NONCE_SIZE);
  let padded: Uint8Array;
  try {
    padded = new Uint8Array(
      await crypto.subtle.decrypt(
        {
          name: 'AES-GCM',
          iv: buf(nonce),
          additionalData: buf(aad(space, room, kind, sha)),
          tagLength: 128
        },
        keys.framesKey,
        buf(envelope.slice(1 + NONCE_SIZE))
      )
    );
  } catch {
    fail(space.error, 'Could not decrypt the frame: wrong key or room, or it was altered');
  }
  return { kind, plaintext: unpad(padded, blockFor(space, kind), space.error) };
}

// ---- JSON ----------------------------------------------------------------------------

function cmp(a: string, b: string): number {
  // Python sorts keys by code point; JS's default sorts by UTF-16 unit (differs for astral).
  const x = Array.from(a);
  const y = Array.from(b);
  for (let i = 0; i < Math.min(x.length, y.length); i++) {
    const d = x[i].codePointAt(0)! - y[i].codePointAt(0)!;
    if (d !== 0) return d;
  }
  return x.length - y.length;
}

function canon(v: unknown, err: ErrorCtor | undefined): string {
  if (v === null) return 'null';
  switch (typeof v) {
    case 'string':
      return JSON.stringify(v);
    case 'boolean':
      return v ? 'true' : 'false';
    case 'number':
      if (!Number.isFinite(v)) fail(err, 'That value cannot be written as JSON');
      return JSON.stringify(v);
    case 'object': {
      if (Array.isArray(v)) return '[' + v.map((x) => canon(x, err)).join(',') + ']';
      const o = v as Record<string, unknown>;
      const keys = Object.keys(o)
        .filter((k) => o[k] !== undefined)
        .sort(cmp);
      return '{' + keys.map((k) => JSON.stringify(k) + ':' + canon(o[k], err)).join(',') + '}';
    }
    default:
      fail(err, 'That value cannot be written as JSON');
  }
}

/** Sorted keys, no whitespace, UTF-8, non-ASCII kept: Python's `json.dumps(sort_keys=True,
 * separators=(",", ":"), ensure_ascii=False)` for strings, numbers, booleans, null, arrays, objects. */
export function canonicalJson(obj: unknown, err?: ErrorCtor): Uint8Array {
  return enc.encode(canon(obj, err));
}

/** Strict UTF-8 JSON; throws `err` (default SealedFormatError) on anything else. */
export function parseJson(data: Uint8Array, err?: ErrorCtor, what = 'The frame is not valid JSON'): unknown {
  try {
    return JSON.parse(dec.decode(data));
  } catch {
    fail(err, what);
  }
}

export async function sealJson(
  space: Space,
  keys: { framesKey: CryptoKey },
  room: string,
  kind: number,
  obj: unknown,
  nonce?: Uint8Array
): Promise<Uint8Array> {
  if (!space.jsonKinds.includes(kind)) fail(space.error, 'That frame kind does not carry JSON');
  return seal(space, keys, room, kind, canonicalJson(obj, space.error), nonce);
}

export async function unsealJson(
  space: Space,
  keys: { framesKey: CryptoKey },
  room: string,
  envelope: Uint8Array
): Promise<{ kind: number; obj: unknown }> {
  const { kind, plaintext } = await unseal(space, keys, room, envelope);
  if (!space.jsonKinds.includes(kind)) fail(space.error, 'That frame kind does not carry JSON');
  return { kind, obj: parseJson(plaintext, space.error) };
}
