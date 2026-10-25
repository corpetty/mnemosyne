// The guest-notes wire format (phase 9), browser half. The other half is
// backend/mnemosyne/services/guest_format.py (its docstring has the rules); backend/tests/vectors/
// guest_v1.json ties them together. Change one, change the other and the vectors. WebCrypto only.

export const FORMAT = 1;
export const AAD_PREFIX = `mnemosyne/guest/${FORMAT}`;
export const NONCE_SIZE = 12;
export const TAG_SIZE = 16;
export const KEY_SIZE = 32;
export const KEY_B64_LEN = 43;
export const ROOM_HEX_LEN = 16;
export const BLOCK = 256;
export const SNAPSHOT_BLOCK = 4096;
export const MAX_FIELD = 255;
export const FRAME_START = [0xde, 0xaf];

const INFO_FRAMES = 'mnemosyne/guest/frames';
const INFO_ROOM = 'mnemosyne/guest/room';

export const Kind = {
  NOTES: 1,
  SNAPSHOT: 2,
  COMMENT: 3,
  META: 4,
  PRESENCE: 5
} as const;
export type Kind = (typeof Kind)[keyof typeof Kind];
export type JsonKind = Exclude<Kind, typeof Kind.NOTES>;
const JSON_KINDS: number[] = [Kind.SNAPSHOT, Kind.COMMENT, Kind.META, Kind.PRESENCE];

export class GuestFormatError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'GuestFormatError';
  }
}

export interface GuestKeys {
  framesKey: CryptoKey;
  /** Raw bytes of the frames key (for tests). */
  framesKeyBytes: Uint8Array;
  roomToken: string;
}

const enc = new TextEncoder();
const dec = new TextDecoder('utf-8', { fatal: true });
const buf = (u: Uint8Array) => u as BufferSource;

// ---- encodings -----------------------------------------------------------------------

const B64URL = /^[A-Za-z0-9_-]*$/;

export function b64urlEncode(data: Uint8Array): string {
  let bin = '';
  for (const b of data) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** Strict: refuses other characters, impossible lengths and non-zero trailing bits. */
export function b64urlDecode(text: string): Uint8Array {
  if (!B64URL.test(text) || text.length % 4 === 1) throw new GuestFormatError('Not valid base64url');
  const std = text.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (text.length % 4)) % 4);
  const bin = atob(std);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  if (b64urlEncode(out) !== text) throw new GuestFormatError('Not valid base64url');
  return out;
}

export function hex(data: Uint8Array): string {
  return Array.from(data, (b) => b.toString(16).padStart(2, '0')).join('');
}

export function fromHex(text: string): Uint8Array {
  if (!/^([0-9a-f]{2})*$/i.test(text)) throw new GuestFormatError('Not valid hex');
  const out = new Uint8Array(text.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(text.slice(i * 2, i * 2 + 2), 16);
  return out;
}

// ---- ids and keys --------------------------------------------------------------------

export function newRoom(): string {
  return hex(crypto.getRandomValues(new Uint8Array(ROOM_HEX_LEN / 2)));
}

export function newKey(): Uint8Array {
  return crypto.getRandomValues(new Uint8Array(KEY_SIZE));
}

export function checkRoom(room: string): string {
  if (typeof room !== 'string' || !/^[0-9a-f]{16}$/.test(room)) {
    throw new GuestFormatError('A room id is 16 lowercase hex characters');
  }
  return room;
}

function checkKey(key: Uint8Array): void {
  if (!(key instanceof Uint8Array) || key.length !== KEY_SIZE) {
    throw new GuestFormatError('A guest key is 32 bytes');
  }
}

export function keyToFragment(key: Uint8Array): string {
  checkKey(key);
  return b64urlEncode(key);
}

export function keyFromFragment(text: string): Uint8Array {
  if (typeof text !== 'string' || text.length !== KEY_B64_LEN) {
    throw new GuestFormatError("That link's key is not valid");
  }
  let key: Uint8Array;
  try {
    key = b64urlDecode(text);
  } catch {
    throw new GuestFormatError("That link's key is not valid");
  }
  if (key.length !== KEY_SIZE) throw new GuestFormatError("That link's key is not valid");
  return key;
}

async function hkdf(key: Uint8Array, info: string): Promise<Uint8Array> {
  checkKey(key);
  const base = await crypto.subtle.importKey('raw', buf(key), 'HKDF', false, ['deriveBits']);
  const bits = await crypto.subtle.deriveBits(
    { name: 'HKDF', hash: 'SHA-256', salt: new Uint8Array(0), info: buf(enc.encode(info)) },
    base,
    256
  );
  return new Uint8Array(bits);
}

export async function deriveKeys(key: Uint8Array): Promise<GuestKeys> {
  const framesKeyBytes = await hkdf(key, INFO_FRAMES);
  const framesKey = await crypto.subtle.importKey('raw', buf(framesKeyBytes), 'AES-GCM', false, [
    'encrypt',
    'decrypt'
  ]);
  return { framesKey, framesKeyBytes, roomToken: b64urlEncode(await hkdf(key, INFO_ROOM)) };
}

// ---- padding, AAD --------------------------------------------------------------------

function checkKind(kind: number): Kind {
  if (!Number.isInteger(kind) || kind < 1 || kind > 5) {
    throw new GuestFormatError(`Unknown frame kind ${String(kind)}`);
  }
  return kind as Kind;
}

export function blockFor(kind: number): number {
  return checkKind(kind) === Kind.SNAPSHOT ? SNAPSHOT_BLOCK : BLOCK;
}

export function aad(room: string, kind: number): Uint8Array {
  return enc.encode(`${AAD_PREFIX}|${checkRoom(room)}|${checkKind(kind)}`);
}

export function pad(data: Uint8Array, block: number): Uint8Array {
  const total = Math.ceil((4 + data.length) / block) * block;
  const out = new Uint8Array(total);
  new DataView(out.buffer).setUint32(0, data.length, false);
  out.set(data, 4);
  return out;
}

export function unpad(padded: Uint8Array, block: number): Uint8Array {
  if (padded.length < 4 || padded.length % block !== 0) {
    throw new GuestFormatError('The padded data has the wrong size');
  }
  const length = new DataView(padded.buffer, padded.byteOffset, 4).getUint32(0, false);
  if (4 + length > padded.length || padded.length - (4 + length) >= block) {
    throw new GuestFormatError('The padded data has a bad length');
  }
  for (let i = 4 + length; i < padded.length; i++) {
    if (padded[i] !== 0) throw new GuestFormatError('The padding is not zeros');
  }
  return padded.slice(4, 4 + length);
}

// ---- sealing -------------------------------------------------------------------------

/** Pad and encrypt into an envelope. `nonce` is for the test vectors only. */
export async function seal(
  keys: GuestKeys,
  room: string,
  kind: number,
  plaintext: Uint8Array,
  nonce?: Uint8Array
): Promise<Uint8Array> {
  const k = checkKind(kind);
  const additional = aad(room, k);
  const padded = pad(plaintext, blockFor(k));
  if (nonce === undefined) nonce = crypto.getRandomValues(new Uint8Array(NONCE_SIZE));
  else if (nonce.length !== NONCE_SIZE) throw new GuestFormatError('A nonce is 12 bytes');
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
  keys: GuestKeys,
  room: string,
  envelope: Uint8Array
): Promise<{ kind: Kind; plaintext: Uint8Array }> {
  checkRoom(room);
  if (envelope.length < 1 + NONCE_SIZE + TAG_SIZE) {
    throw new GuestFormatError('The data is too short to be a guest frame');
  }
  const kind = checkKind(envelope[0]);
  const nonce = envelope.slice(1, 1 + NONCE_SIZE);
  let padded: Uint8Array;
  try {
    padded = new Uint8Array(
      await crypto.subtle.decrypt(
        { name: 'AES-GCM', iv: buf(nonce), additionalData: buf(aad(room, kind)), tagLength: 128 },
        keys.framesKey,
        buf(envelope.slice(1 + NONCE_SIZE))
      )
    );
  } catch {
    throw new GuestFormatError('Could not decrypt the frame: wrong key or room, or it was altered');
  }
  return { kind, plaintext: unpad(padded, blockFor(kind)) };
}

// ---- notes frames --------------------------------------------------------------------

/** UTF-8 of `text`, cut to at most `limit` bytes at a character boundary. */
function truncate(text: string, limit: number = MAX_FIELD): Uint8Array {
  const data = enc.encode(text);
  if (data.length <= limit) return data;
  let end = limit;
  // Back up over continuation bytes (10xxxxxx) so no character is split.
  while (end > 0 && (data[end] & 0xc0) === 0x80) end--;
  return data.slice(0, end);
}

function checkFrame(frame: Uint8Array): void {
  if (frame.length === 0 || !FRAME_START.includes(frame[0])) {
    throw new GuestFormatError('A notes frame starts with 0xDE or 0xAF');
  }
}

export function notesPlaintext(gid: string, name: string, frame: Uint8Array): Uint8Array {
  const gidB = enc.encode(gid);
  if (gidB.length > MAX_FIELD) throw new GuestFormatError('A guest id is at most 255 bytes');
  const nameB = truncate(name);
  checkFrame(frame);
  const out = new Uint8Array(2 + gidB.length + nameB.length + frame.length);
  let pos = 0;
  out[pos++] = gidB.length;
  out.set(gidB, pos);
  pos += gidB.length;
  out[pos++] = nameB.length;
  out.set(nameB, pos);
  pos += nameB.length;
  out.set(frame, pos);
  return out;
}

export function parseNotes(plaintext: Uint8Array): { gid: string; name: string; frame: Uint8Array } {
  let pos = 0;
  const fields: string[] = [];
  for (let i = 0; i < 2; i++) {
    if (pos >= plaintext.length) throw new GuestFormatError('The notes frame is truncated');
    const n = plaintext[pos];
    const raw = plaintext.slice(pos + 1, pos + 1 + n);
    if (raw.length !== n) throw new GuestFormatError('The notes frame is truncated');
    try {
      fields.push(dec.decode(raw));
    } catch {
      throw new GuestFormatError('The notes frame has a bad name');
    }
    pos += 1 + n;
  }
  const frame = plaintext.slice(pos);
  checkFrame(frame);
  return { gid: fields[0], name: fields[1], frame };
}

export async function sealNotes(
  keys: GuestKeys,
  room: string,
  gid: string,
  name: string,
  frame: Uint8Array,
  nonce?: Uint8Array
): Promise<Uint8Array> {
  return seal(keys, room, Kind.NOTES, notesPlaintext(gid, name, frame), nonce);
}

export async function unsealNotes(
  keys: GuestKeys,
  room: string,
  envelope: Uint8Array
): Promise<{ gid: string; name: string; frame: Uint8Array }> {
  const { kind, plaintext } = await unseal(keys, room, envelope);
  if (kind !== Kind.NOTES) throw new GuestFormatError('That is not a notes frame');
  return parseNotes(plaintext);
}

// ---- JSON frames ---------------------------------------------------------------------

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

function canon(v: unknown): string {
  if (v === null) return 'null';
  switch (typeof v) {
    case 'string':
      return JSON.stringify(v);
    case 'boolean':
      return v ? 'true' : 'false';
    case 'number':
      if (!Number.isFinite(v)) throw new GuestFormatError('That value cannot be written as JSON');
      return JSON.stringify(v);
    case 'object': {
      if (Array.isArray(v)) return '[' + v.map(canon).join(',') + ']';
      const o = v as Record<string, unknown>;
      const keys = Object.keys(o)
        .filter((k) => o[k] !== undefined)
        .sort(cmp);
      return '{' + keys.map((k) => JSON.stringify(k) + ':' + canon(o[k])).join(',') + '}';
    }
    default:
      throw new GuestFormatError('That value cannot be written as JSON');
  }
}

/** Sorted keys, no whitespace, UTF-8, non-ASCII kept: Python's `json.dumps(sort_keys=True,
 * separators=(",", ":"), ensure_ascii=False)` for strings, numbers, booleans, null, arrays, objects. */
export function canonicalJson(obj: unknown): Uint8Array {
  return enc.encode(canon(obj));
}

export async function sealJson(
  keys: GuestKeys,
  room: string,
  kind: JsonKind,
  obj: unknown,
  nonce?: Uint8Array
): Promise<Uint8Array> {
  if (!JSON_KINDS.includes(kind)) throw new GuestFormatError('That frame kind does not carry JSON');
  return seal(keys, room, kind, canonicalJson(obj), nonce);
}

export async function unsealJson(
  keys: GuestKeys,
  room: string,
  envelope: Uint8Array
): Promise<{ kind: Kind; obj: unknown }> {
  const { kind, plaintext } = await unseal(keys, room, envelope);
  if (!JSON_KINDS.includes(kind)) throw new GuestFormatError('That frame kind does not carry JSON');
  try {
    return { kind, obj: JSON.parse(dec.decode(plaintext)) };
  } catch {
    throw new GuestFormatError('The frame is not valid JSON');
  }
}
