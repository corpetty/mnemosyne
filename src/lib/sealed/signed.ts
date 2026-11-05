// Live-publication frames (docs/plans/2026-11-05-live-publications.md, "Data and formats"), browser
// half. The other half is backend/mnemosyne/services/pub_live_format.py (its docstring has the
// rules); backend/tests/vectors/publication_live_v1.json ties them together. Change one, change
// the other and the vectors. Ed25519 and X25519 from @noble/curves (pinned; WebCrypto's curves are
// too new for the readers' browsers), AES-GCM, HKDF and SHA-256 from WebCrypto. Imports nothing
// from the app (the viewer entry bundles it).
//
// Rules, exactly as implemented:
// - the envelope is envelope.ts's in the PUB_LIVE space: kind(1) || nonce(12) || AES-256-GCM(
//   framesKey, nonce, padded), padded to a multiple of 256 bytes for EVERY kind, AAD = UTF-8 of
//   `mnemosyne/pub-live/1|<room>|<kind>`; framesKey = HKDF-SHA256(link key, salt "", info
//   "mnemosyne/pub/frames"), roomToken = b64url(HKDF-SHA256(link key, salt "", info
//   "mnemosyne/pub/room")). Kinds: VERSION=1, THREADS=2, META=3 (owner, signed), COMMENT=4 (reader,
//   boxed and signed).
// - owner frames: plaintext = sig(64) || body, body = canonical JSON of an object, sig = Ed25519
//   (RFC 8032, strict verification) by the owner's key over UTF-8(`mnemosyne/pub-live/1|<room>|
//   <kind>|`) || body. Opening verifies against the manifest's `live.sign`; the body is parsed as it
//   came, not re-canonicalised.
// - COMMENT: plaintext = eph(32) || nonce(12) || AES-256-GCM(boxKey, nonce, content) (tag at the
//   end; content not padded, the envelope pads); eph = a fresh X25519 public key per frame; shared =
//   X25519(eph private, owner box public), all-zero refused; boxKey = HKDF-SHA256(shared, salt =
//   UTF-8 of the 16-character room id, info "mnemosyne/pub/box"); AAD = UTF-8 of
//   `mnemosyne/pub-live/1|<room>|4|<eph as 64 lowercase hex>`. content = sig(64) || readerPub(32) ||
//   op, op = canonical JSON of an object, sig = the reader's Ed25519 over UTF-8(
//   `mnemosyne/pub-live/1|<room>|4|`) || readerPub || op.
// - rid = the first 16 lowercase hex characters of SHA-256(readerPub, the raw 32 bytes).
// - private keys are raw 32 bytes (Ed25519 seed, X25519 scalar before clamping); public keys raw 32
//   bytes, 64 lowercase hex in `Manifest.live` ({sign, box}).

import { ed25519, x25519 } from '@noble/curves/ed25519.js';
import * as env from './envelope.js';

export const FORMAT = 1;
export const AAD_PREFIX = `mnemosyne/pub-live/${FORMAT}`;
export const INFO_BOX = 'mnemosyne/pub/box';
export const SIG_SIZE = 64;
export const PUBLIC_SIZE = 32;
export const PRIVATE_SIZE = 32;
export const RID_HEX_LEN = 16;

export const PubKind = {
  VERSION: 1,
  THREADS: 2,
  META: 3,
  COMMENT: 4
} as const;
export type PubKind = (typeof PubKind)[keyof typeof PubKind];
export type OwnerKind = Exclude<PubKind, typeof PubKind.COMMENT>;
const OWNER_KINDS: number[] = [PubKind.VERSION, PubKind.THREADS, PubKind.META];

export class PubLiveFormatError extends env.SealedFormatError {
  constructor(message: string) {
    super(message);
    this.name = 'PubLiveFormatError';
  }
}

/** The live-publication space: its own AAD prefix and HKDF infos, 256-byte blocks for every kind. */
export const PUB_LIVE: env.Space = {
  label: 'publication',
  aadPrefix: AAD_PREFIX,
  infoFrames: 'mnemosyne/pub/frames',
  infoRoom: 'mnemosyne/pub/room',
  kinds: [1, 2, 3, 4],
  blocks: {},
  jsonKinds: [],
  error: PubLiveFormatError
};

const enc = new TextEncoder();
const dec = new TextDecoder('utf-8', { fatal: true });

function fail(message: string): never {
  throw new PubLiveFormatError(message);
}

// ---- keys ----------------------------------------------------------------------------

function checkPrivate(raw: Uint8Array): Uint8Array {
  if (!(raw instanceof Uint8Array) || raw.length !== PRIVATE_SIZE) fail('A private key is 32 bytes');
  return raw;
}

function checkPublic(raw: Uint8Array): Uint8Array {
  if (!(raw instanceof Uint8Array) || raw.length !== PUBLIC_SIZE) fail('A public key is 32 bytes');
  return raw;
}

/** 32 random bytes: an Ed25519 seed or an X25519 scalar, whichever it is used as. */
export function newPrivate(): Uint8Array {
  return crypto.getRandomValues(new Uint8Array(PRIVATE_SIZE));
}

/** The Ed25519 public key of a raw signing key. */
export function signPublic(privateKey: Uint8Array): Uint8Array {
  return ed25519.getPublicKey(checkPrivate(privateKey));
}

/** The X25519 public key of a raw box key. */
export function boxPublic(privateKey: Uint8Array): Uint8Array {
  return x25519.getPublicKey(checkPrivate(privateKey));
}

/** A raw 64-byte Ed25519 signature. */
export function ed25519Sign(privateKey: Uint8Array, message: Uint8Array): Uint8Array {
  return ed25519.sign(message, checkPrivate(privateKey));
}

/** Strict RFC 8032 verification (not ZIP-215), as OpenSSL does on the owner's side. */
export function ed25519Verify(publicKey: Uint8Array, sig: Uint8Array, message: Uint8Array): boolean {
  if (!(sig instanceof Uint8Array) || sig.length !== SIG_SIZE) return false;
  try {
    return ed25519.verify(sig, message, checkPublic(publicKey), { zip215: false });
  } catch {
    return false;
  }
}

/** The X25519 shared secret of a raw private and a raw public key (an all-zero result refused). */
export function x25519Shared(privateKey: Uint8Array, publicKey: Uint8Array): Uint8Array {
  let shared: Uint8Array;
  try {
    shared = x25519.getSharedSecret(checkPrivate(privateKey), checkPublic(publicKey));
  } catch (e) {
    if (e instanceof PubLiveFormatError) throw e;
    fail('That key cannot be used for a box');
  }
  if (shared.every((b) => b === 0)) fail('That key cannot be used for a box');
  return shared;
}

/** A reader's id: the first 16 hex characters of SHA-256 of their Ed25519 public key. */
export async function ridOf(readerPub: Uint8Array): Promise<string> {
  return (await env.sha256Hex(checkPublic(readerPub))).slice(0, RID_HEX_LEN);
}

/** The public halves a manifest's `live` carries, as raw bytes; null when absent or malformed. */
export function parseLiveKeys(live: unknown): { sign: Uint8Array; box: Uint8Array } | null {
  if (!live || typeof live !== 'object') return null;
  const { sign, box } = live as Record<string, unknown>;
  const ok = (v: unknown): v is string => typeof v === 'string' && /^[0-9a-f]{64}$/.test(v);
  if (!ok(sign) || !ok(box)) return null;
  return { sign: env.fromHex(sign), box: env.fromHex(box) };
}

// ---- derivation and the envelope -----------------------------------------------------

/** The frames key and room token of a publication's link key in the PUB_LIVE space. */
export async function deriveLiveKeys(key: Uint8Array): Promise<env.SpaceKeys> {
  return env.deriveSpaceKeys(PUB_LIVE, key);
}

export function aad(room: string, kind: number): Uint8Array {
  return env.aad(PUB_LIVE, room, kind);
}

/** The bare envelope (no signature, no box): for the vectors and the refusal tests. */
export function seal(
  keys: { framesKey: CryptoKey },
  room: string,
  kind: number,
  plaintext: Uint8Array,
  nonce?: Uint8Array
): Promise<Uint8Array> {
  return env.seal(PUB_LIVE, keys, room, kind, plaintext, nonce);
}

export async function unseal(
  keys: { framesKey: CryptoKey },
  room: string,
  envelope: Uint8Array
): Promise<{ kind: PubKind; plaintext: Uint8Array }> {
  const { kind, plaintext } = await env.unseal(PUB_LIVE, keys, room, envelope);
  return { kind: kind as PubKind, plaintext };
}

/** What a signature's subject starts with: UTF-8 `mnemosyne/pub-live/1|<room>|<kind>|`. */
function prefix(room: string, kind: number): Uint8Array {
  env.checkRoom(room, PubLiveFormatError);
  return enc.encode(`${AAD_PREFIX}|${room}|${env.checkKind(PUB_LIVE, kind)}|`);
}

function jsonObject(obj: unknown): Uint8Array {
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) fail("A live frame's body is a JSON object");
  return env.canonicalJson(obj, PubLiveFormatError);
}

function parseObject(data: Uint8Array): Record<string, unknown> {
  let value: unknown;
  try {
    value = JSON.parse(dec.decode(data));
  } catch {
    fail('The frame is not valid JSON');
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail("A live frame's body is a JSON object");
  return value as Record<string, unknown>;
}

// ---- owner frames: VERSION, THREADS, META --------------------------------------------

function ownerKind(kind: number): OwnerKind {
  env.checkKind(PUB_LIVE, kind);
  if (!OWNER_KINDS.includes(kind)) fail('Only VERSION, THREADS and META are signed by the owner');
  return kind as OwnerKind;
}

/** The bytes an owner frame's signature covers. */
export function signedMessage(room: string, kind: number, body: Uint8Array): Uint8Array {
  return env.concat(prefix(room, ownerKind(kind)), body);
}

/** sig(64) || body: what goes inside the envelope of an owner frame. */
export function signedPlaintext(room: string, kind: number, obj: unknown, signPrivate: Uint8Array): Uint8Array {
  const body = jsonObject(obj);
  return env.concat(ed25519Sign(signPrivate, signedMessage(room, kind, body)), body);
}

/** Seal an owner frame signed by the publication's signing key (the owner's side; tests here).
 * `nonce`: vectors only. */
export function signFrame(
  keys: { framesKey: CryptoKey },
  room: string,
  kind: OwnerKind,
  obj: unknown,
  signPrivate: Uint8Array,
  nonce?: Uint8Array
): Promise<Uint8Array> {
  const k = ownerKind(kind);
  return seal(keys, room, k, signedPlaintext(room, k, obj, signPrivate), nonce);
}

/**
 * Open an owner frame and verify it against the manifest's `live.sign`. Throws PubLiveFormatError
 * for anything that does not verify (the viewer drops such frames), for a COMMENT, and, with
 * `kind`, for any other kind.
 */
export async function openSigned(
  keys: { framesKey: CryptoKey },
  room: string,
  envelope: Uint8Array,
  signPub: Uint8Array,
  kind?: OwnerKind
): Promise<{ kind: OwnerKind; obj: Record<string, unknown> }> {
  const { kind: k, plaintext } = await unseal(keys, room, envelope);
  ownerKind(k);
  if (kind !== undefined && k !== kind) fail('That is not the expected kind of frame');
  if (plaintext.length < SIG_SIZE) fail('The frame is too short to be signed');
  const sig = plaintext.slice(0, SIG_SIZE);
  const body = plaintext.slice(SIG_SIZE);
  if (!ed25519Verify(signPub, sig, signedMessage(room, k, body))) {
    fail("The frame is not signed by this publication's owner");
  }
  return { kind: k as OwnerKind, obj: parseObject(body) };
}

// ---- reader frames: COMMENT, boxed to the owner and signed by the reader -------------

/** HKDF-SHA256(shared, salt = the room id's UTF-8, info = mnemosyne/pub/box). */
export function boxKey(shared: Uint8Array, room: string): Promise<Uint8Array> {
  env.checkRoom(room, PubLiveFormatError);
  return env.hkdf(shared, INFO_BOX, enc.encode(room));
}

export function boxAad(room: string, ephPub: Uint8Array): Uint8Array {
  return env.concat(prefix(room, PubKind.COMMENT), enc.encode(env.hex(checkPublic(ephPub))));
}

/** The bytes a reader's signature covers. */
export function readerMessage(room: string, readerPub: Uint8Array, op: Uint8Array): Uint8Array {
  return env.concat(prefix(room, PubKind.COMMENT), checkPublic(readerPub), op);
}

export interface BoxOptions {
  /** The ephemeral X25519 key (vectors only; else fresh per frame). */
  ephPrivate?: Uint8Array;
  /** The box's AES-GCM nonce (vectors only). */
  boxNonce?: Uint8Array;
  /** The envelope's nonce (vectors only). */
  nonce?: Uint8Array;
}

/** eph(32) || nonce(12) || AES-GCM(content): what goes inside a COMMENT envelope. */
export async function boxPlaintext(
  room: string,
  op: unknown,
  readerPrivate: Uint8Array,
  ownerBoxPub: Uint8Array,
  opts: BoxOptions = {}
): Promise<Uint8Array> {
  const opBytes = jsonObject(op);
  const readerPub = signPublic(readerPrivate);
  const content = env.concat(ed25519Sign(readerPrivate, readerMessage(room, readerPub, opBytes)), readerPub, opBytes);
  const ephPrivate = opts.ephPrivate ?? newPrivate();
  const ephPub = boxPublic(ephPrivate);
  const aes = await env.aesKey(await boxKey(x25519Shared(ephPrivate, ownerBoxPub), room));
  let boxNonce = opts.boxNonce;
  if (boxNonce === undefined) boxNonce = crypto.getRandomValues(new Uint8Array(env.NONCE_SIZE));
  else if (boxNonce.length !== env.NONCE_SIZE) fail('A nonce is 12 bytes');
  const sealed = new Uint8Array(
    await crypto.subtle.encrypt(
      { name: 'AES-GCM', iv: env.buf(boxNonce), additionalData: env.buf(boxAad(room, ephPub)), tagLength: 128 },
      aes,
      env.buf(content)
    )
  );
  return env.concat(ephPub, boxNonce, sealed);
}

/** A reader's COMMENT envelope: `op` sealed to the owner's box key and signed by the reader. */
export async function boxOp(
  keys: { framesKey: CryptoKey },
  room: string,
  op: unknown,
  readerPrivate: Uint8Array,
  ownerBoxPub: Uint8Array,
  opts: BoxOptions = {}
): Promise<Uint8Array> {
  const plain = await boxPlaintext(room, op, readerPrivate, ownerBoxPub, opts);
  return seal(keys, room, PubKind.COMMENT, plain, opts.nonce);
}

export interface ReaderOp {
  rid: string;
  readerPub: Uint8Array;
  op: Record<string, unknown>;
}

/**
 * Open a reader's COMMENT with the owner's box key and check the reader's signature (the owner's
 * side; here for tests). Throws PubLiveFormatError for any other kind, another owner's box, an
 * altered frame or a signature that does not match the readerPub inside.
 */
export async function openBox(
  keys: { framesKey: CryptoKey },
  room: string,
  envelope: Uint8Array,
  ownerBoxPrivate: Uint8Array
): Promise<ReaderOp> {
  const { kind, plaintext: plain } = await unseal(keys, room, envelope);
  if (kind !== PubKind.COMMENT) fail('That is not a comment frame');
  if (plain.length < PUBLIC_SIZE + env.NONCE_SIZE + env.TAG_SIZE + SIG_SIZE + PUBLIC_SIZE) {
    fail('The comment frame is too short');
  }
  const ephPub = plain.slice(0, PUBLIC_SIZE);
  const boxNonce = plain.slice(PUBLIC_SIZE, PUBLIC_SIZE + env.NONCE_SIZE);
  const aes = await env.aesKey(await boxKey(x25519Shared(ownerBoxPrivate, ephPub), room));
  let content: Uint8Array;
  try {
    content = new Uint8Array(
      await crypto.subtle.decrypt(
        { name: 'AES-GCM', iv: env.buf(boxNonce), additionalData: env.buf(boxAad(room, ephPub)), tagLength: 128 },
        aes,
        env.buf(plain.slice(PUBLIC_SIZE + env.NONCE_SIZE))
      )
    );
  } catch {
    fail('Could not open the comment: it is not for this owner, or it was altered');
  }
  if (content.length < SIG_SIZE + PUBLIC_SIZE) fail('The comment frame is too short');
  const sig = content.slice(0, SIG_SIZE);
  const readerPub = content.slice(SIG_SIZE, SIG_SIZE + PUBLIC_SIZE);
  const op = content.slice(SIG_SIZE + PUBLIC_SIZE);
  if (!ed25519Verify(readerPub, sig, readerMessage(room, readerPub, op))) {
    fail('The comment is not signed by the key it carries');
  }
  return { rid: await ridOf(readerPub), readerPub, op: parseObject(op) };
}
