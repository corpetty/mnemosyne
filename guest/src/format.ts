// The guest-notes wire format (phase 9), browser half. The other half is
// backend/mnemosyne/services/guest_format.py (its docstring has the rules); backend/tests/vectors/
// guest_v1.json ties them together. Change one, change the other and the vectors. WebCrypto only.
//
// The envelope (key derivation, padding, AAD, AES-GCM, canonical JSON) is shared with live
// publications in src/lib/sealed/envelope.ts; this module is the guest space (`GUEST`) over it,
// plus what only guest notes have (the link fragment, notes frames, media).
//
// MEDIA (kind 6, an image of the notes) is raw bytes (at most MEDIA_MAX) padded to MEDIA_BLOCK, and its
// AAD has a fourth field: `mnemosyne/guest/1|<room>|6|<sha>`, the plaintext's SHA-256 as 64 lowercase
// hex characters (required for MEDIA, refused for other kinds). The relay stores a blob under
// `mediaName` = hex(HMAC-SHA256(mediaNameKey, the sha's 64 ASCII characters)) with mediaNameKey =
// HKDF-SHA256(key, salt "", info "mnemosyne/guest/media-name"): an opaque name every key holder can
// compute. `unsealMedia` checks sha256(bytes) === sha. `seal`/`unseal` take the sha as a last argument.

import * as env from '$lib/sealed/envelope';

export const FORMAT = 1;
export const AAD_PREFIX = `mnemosyne/guest/${FORMAT}`;
export const NONCE_SIZE = env.NONCE_SIZE;
export const TAG_SIZE = env.TAG_SIZE;
export const KEY_SIZE = env.KEY_SIZE;
export const KEY_B64_LEN = 43;
export const ROOM_HEX_LEN = env.ROOM_HEX_LEN;
export const BLOCK = env.BLOCK;
export const SNAPSHOT_BLOCK = 4096;
export const MEDIA_BLOCK = 65536;
export const MEDIA_MAX = 2_000_000;
export const MAX_FIELD = 255;
export const FRAME_START = [0xde, 0xaf];

export const Kind = {
  NOTES: 1,
  SNAPSHOT: 2,
  COMMENT: 3,
  META: 4,
  PRESENCE: 5,
  MEDIA: 6
} as const;
export type Kind = (typeof Kind)[keyof typeof Kind];
export type JsonKind = Exclude<Kind, typeof Kind.NOTES | typeof Kind.MEDIA>;

export class GuestFormatError extends env.SealedFormatError {
  constructor(message: string) {
    super(message);
    this.name = 'GuestFormatError';
  }
}

/** The guest space, throwing GuestFormatError. */
const SPACE: env.Space = { ...env.GUEST, error: GuestFormatError };

export interface GuestKeys {
  framesKey: CryptoKey;
  /** Raw bytes of the frames key (for tests). */
  framesKeyBytes: Uint8Array;
  roomToken: string;
  /** HMAC-SHA256 key for `mediaName`. */
  mediaNameKey: CryptoKey;
  /** Raw bytes of the media name key (for tests). */
  mediaNameKeyBytes: Uint8Array;
}

const enc = new TextEncoder();
const dec = new TextDecoder('utf-8', { fatal: true });

// ---- encodings -----------------------------------------------------------------------

export const b64urlEncode = env.b64urlEncode;

/** Strict: refuses other characters, impossible lengths and non-zero trailing bits. */
export function b64urlDecode(text: string): Uint8Array {
  return env.b64urlDecode(text, GuestFormatError);
}

export const hex = env.hex;

export function fromHex(text: string): Uint8Array {
  return env.fromHex(text, GuestFormatError);
}

// ---- ids and keys --------------------------------------------------------------------

export const newRoom = env.newRoom;
export const newKey = env.newKey;

export function checkRoom(room: string): string {
  return env.checkRoom(room, GuestFormatError);
}

export function keyToFragment(key: Uint8Array): string {
  env.checkKey(SPACE, key);
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

export async function deriveKeys(key: Uint8Array): Promise<GuestKeys> {
  const k = await env.deriveSpaceKeys(SPACE, key);
  return {
    framesKey: k.framesKey,
    framesKeyBytes: k.framesKeyBytes,
    roomToken: k.roomToken,
    mediaNameKey: k.mediaNameKey!,
    mediaNameKeyBytes: k.mediaNameKeyBytes!
  };
}

export function checkSha(sha: string): string {
  return env.checkSha(sha, GuestFormatError);
}

/** The name a room stores the blob of plaintext `sha` under (64 hex characters). */
export async function mediaName(keys: GuestKeys, sha: string): Promise<string> {
  const mac = await crypto.subtle.sign('HMAC', keys.mediaNameKey, env.buf(enc.encode(checkSha(sha))));
  return hex(new Uint8Array(mac));
}

// ---- padding, AAD --------------------------------------------------------------------

export function blockFor(kind: number): number {
  return env.blockFor(SPACE, kind);
}

/** `sha` (the plaintext's SHA-256, hex) is required for MEDIA and refused for other kinds. */
export function aad(room: string, kind: number, sha?: string): Uint8Array {
  return env.aad(SPACE, room, kind, sha);
}

export const pad = env.pad;

export function unpad(padded: Uint8Array, block: number): Uint8Array {
  return env.unpad(padded, block, GuestFormatError);
}

// ---- sealing -------------------------------------------------------------------------

/** Pad and encrypt into an envelope. `nonce` is for the test vectors only. */
export async function seal(
  keys: GuestKeys,
  room: string,
  kind: number,
  plaintext: Uint8Array,
  nonce?: Uint8Array,
  sha?: string
): Promise<Uint8Array> {
  return env.seal(SPACE, keys, room, kind, plaintext, nonce, sha);
}

export async function unseal(
  keys: GuestKeys,
  room: string,
  envelope: Uint8Array,
  sha?: string
): Promise<{ kind: Kind; plaintext: Uint8Array }> {
  const { kind, plaintext } = await env.unseal(SPACE, keys, room, envelope, sha);
  return { kind: kind as Kind, plaintext };
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

/** Sorted keys, no whitespace, UTF-8, non-ASCII kept: Python's `json.dumps(sort_keys=True,
 * separators=(",", ":"), ensure_ascii=False)` for strings, numbers, booleans, null, arrays, objects. */
export function canonicalJson(obj: unknown): Uint8Array {
  return env.canonicalJson(obj, GuestFormatError);
}

export async function sealJson(
  keys: GuestKeys,
  room: string,
  kind: JsonKind,
  obj: unknown,
  nonce?: Uint8Array
): Promise<Uint8Array> {
  return env.sealJson(SPACE, keys, room, kind, obj, nonce);
}

export async function unsealJson(
  keys: GuestKeys,
  room: string,
  envelope: Uint8Array
): Promise<{ kind: Kind; obj: unknown }> {
  const { kind, obj } = await env.unsealJson(SPACE, keys, room, envelope);
  return { kind: kind as Kind, obj };
}

// ---- media frames --------------------------------------------------------------------

/** Seal an image's bytes under the sha they must hash to (checked here too). */
export async function sealMedia(
  keys: GuestKeys,
  room: string,
  sha: string,
  data: Uint8Array,
  nonce?: Uint8Array
): Promise<Uint8Array> {
  if (data.length > MEDIA_MAX) throw new GuestFormatError(`A media blob is at most ${MEDIA_MAX} bytes`);
  if ((await env.sha256Hex(data)) !== checkSha(sha)) {
    throw new GuestFormatError('The media bytes do not match their sha');
  }
  return seal(keys, room, Kind.MEDIA, data, nonce, sha);
}

/** The bytes of the blob sealed under `sha`; refuses another kind, a wrong sha, and bytes that do not
 * hash to `sha`. */
export async function unsealMedia(
  keys: GuestKeys,
  room: string,
  sha: string,
  envelope: Uint8Array
): Promise<Uint8Array> {
  checkSha(sha);
  if (envelope.length > 0 && envelope[0] !== Kind.MEDIA) {
    throw new GuestFormatError('That is not a media frame');
  }
  const { plaintext } = await unseal(keys, room, envelope, sha);
  if (plaintext.length > MEDIA_MAX) throw new GuestFormatError(`A media blob is at most ${MEDIA_MAX} bytes`);
  if ((await env.sha256Hex(plaintext)) !== sha) {
    throw new GuestFormatError('The media bytes do not match their sha');
  }
  return plaintext;
}
