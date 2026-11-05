// @vitest-environment node
// Node has crypto.subtle globally (jsdom, the default here, does not).
// The envelope across spaces. The guest space's bytes are pinned by guest/src/format.test.ts
// (guest_v1.json); here: what the Space parameter changes and what it must keep apart.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
  BLOCK,
  GUEST,
  SealedFormatError,
  aad,
  b64urlDecode,
  blockFor,
  canonicalJson,
  deriveSpaceKeys,
  fromHex,
  hex,
  hkdf,
  newKey,
  newRoom,
  pad,
  parseJson,
  seal,
  sealJson,
  unpad,
  unseal,
  unsealJson,
  type Space
} from './envelope';

const guestVec = JSON.parse(
  readFileSync(
    fileURLToPath(new URL('../../../backend/tests/vectors/guest_v1.json', import.meta.url)),
    'utf-8'
  )
);

class OtherError extends SealedFormatError {}

const OTHER: Space = {
  label: 'other',
  aadPrefix: 'mnemosyne/other/1',
  infoFrames: 'mnemosyne/other/frames',
  infoRoom: 'mnemosyne/other/room',
  kinds: [1, 2],
  blocks: { 2: 1024 },
  jsonKinds: [2],
  error: OtherError
};

const enc = new TextEncoder();

describe('the guest space', () => {
  it('derives the guest vectors', async () => {
    const keys = await deriveSpaceKeys(GUEST, b64urlDecode(guestVec.key_b64url));
    expect(hex(keys.framesKeyBytes)).toBe(guestVec.frames_key_hex);
    expect(keys.roomToken).toBe(guestVec.room_token);
    expect(hex(keys.mediaNameKeyBytes!)).toBe(guestVec.media_name_key_hex);
  });

  it('has a media kind with a sha in the AAD', () => {
    const room = newRoom();
    const sha = 'ab'.repeat(32);
    expect(new TextDecoder().decode(aad(GUEST, room, 6, sha))).toBe(`mnemosyne/guest/1|${room}|6|${sha}`);
    expect(() => aad(GUEST, room, 6)).toThrow(SealedFormatError);
    expect(() => aad(GUEST, room, 1, sha)).toThrow(SealedFormatError);
  });
});

describe('another space', () => {
  it('derives other keys from the same link key and has no media name key', async () => {
    const key = newKey();
    const a = await deriveSpaceKeys(GUEST, key);
    const b = await deriveSpaceKeys(OTHER, key);
    expect(hex(b.framesKeyBytes)).not.toBe(hex(a.framesKeyBytes));
    expect(b.roomToken).not.toBe(a.roomToken);
    expect(hex(b.framesKeyBytes)).toBe(hex(await hkdf(key, 'mnemosyne/other/frames')));
    expect(b.mediaNameKey).toBeUndefined();
  });

  it('binds its own prefix and kinds into the AAD', () => {
    const room = newRoom();
    expect(new TextDecoder().decode(aad(OTHER, room, 2))).toBe(`mnemosyne/other/1|${room}|2`);
    expect(() => aad(OTHER, room, 3)).toThrow(OtherError);
    expect(() => aad(OTHER, 'nope', 1)).toThrow(OtherError);
  });

  it('pads per kind', () => {
    expect(blockFor(OTHER, 1)).toBe(BLOCK);
    expect(blockFor(OTHER, 2)).toBe(1024);
    expect(() => blockFor(OTHER, 6)).toThrow(OtherError);
  });

  it('round-trips and never opens in the other space, even under the same frames key', async () => {
    const key = newKey();
    const room = newRoom();
    const keys = await deriveSpaceKeys(OTHER, key);
    const env = await seal(OTHER, keys, room, 1, enc.encode('hello'));
    expect(env.length).toBe(1 + 12 + BLOCK + 16);
    const out = await unseal(OTHER, keys, room, env);
    expect(out.kind).toBe(1);
    expect(new TextDecoder().decode(out.plaintext)).toBe('hello');
    // The guest space with the same AES key still refuses: the AAD prefix differs.
    await expect(unseal(GUEST, keys, room, env)).rejects.toBeInstanceOf(SealedFormatError);
    await expect(unseal(OTHER, keys, newRoom(), env)).rejects.toBeInstanceOf(OtherError);
    const flipped = env.slice();
    flipped[20] ^= 1;
    await expect(unseal(OTHER, keys, room, flipped)).rejects.toThrow(/Could not decrypt/);
  });

  it('names the space in its messages', async () => {
    const keys = await deriveSpaceKeys(OTHER, newKey());
    await expect(deriveSpaceKeys(OTHER, new Uint8Array(31))).rejects.toThrow('A other key is 32 bytes');
    await expect(unseal(OTHER, keys, newRoom(), new Uint8Array(10))).rejects.toThrow(
      'The data is too short to be a other frame'
    );
  });

  it('carries JSON only in its JSON kinds', async () => {
    const room = newRoom();
    const keys = await deriveSpaceKeys(OTHER, newKey());
    const env = await sealJson(OTHER, keys, room, 2, { b: 1, a: 'é' });
    expect(env.length).toBe(1 + 12 + 1024 + 16);
    expect((await unsealJson(OTHER, keys, room, env)).obj).toEqual({ a: 'é', b: 1 });
    await expect(sealJson(OTHER, keys, room, 1, {})).rejects.toBeInstanceOf(OtherError);
    const raw = await seal(OTHER, keys, room, 1, enc.encode('{}'));
    await expect(unsealJson(OTHER, keys, room, raw)).rejects.toBeInstanceOf(OtherError);
  });
});

describe('helpers', () => {
  it('pads and unpads with the given error', () => {
    const p = pad(enc.encode('abc'), 256);
    expect(p.length).toBe(256);
    expect(new TextDecoder().decode(unpad(p, 256))).toBe('abc');
    expect(() => unpad(new Uint8Array(100), 256, OtherError)).toThrow(OtherError);
    expect(() => unpad(new Uint8Array(100), 256)).toThrow(SealedFormatError);
  });

  it('writes canonical JSON as Python does', () => {
    expect(new TextDecoder().decode(canonicalJson({ b: [1, true, null], a: 'x', c: undefined }))).toBe(
      '{"a":"x","b":[1,true,null]}'
    );
    expect(() => canonicalJson({ a: Infinity }, OtherError)).toThrow(OtherError);
  });

  it('parses strict UTF-8 JSON', () => {
    expect(parseJson(enc.encode('{"a":1}'))).toEqual({ a: 1 });
    expect(() => parseJson(Uint8Array.from([0xff]))).toThrow(SealedFormatError);
  });

  it('hex round-trips and refuses junk', () => {
    expect(hex(fromHex('00ff10'))).toBe('00ff10');
    expect(() => fromHex('0g', OtherError)).toThrow(OtherError);
  });
});
