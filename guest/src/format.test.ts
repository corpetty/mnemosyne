// @vitest-environment node
// Node has crypto.subtle globally (jsdom, the default here, does not).
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { beforeAll, describe, expect, it } from 'vitest';
import {
  GuestFormatError,
  Kind,
  MEDIA_BLOCK,
  MEDIA_MAX,
  aad,
  b64urlDecode,
  b64urlEncode,
  blockFor,
  canonicalJson,
  deriveKeys,
  fromHex,
  hex,
  keyFromFragment,
  keyToFragment,
  mediaName,
  newKey,
  newRoom,
  notesPlaintext,
  parseNotes,
  seal,
  sealJson,
  sealMedia,
  sealNotes,
  unseal,
  unsealJson,
  unsealMedia,
  unsealNotes,
  unpad,
  type GuestKeys
} from './format';

const vec = JSON.parse(
  readFileSync(
    fileURLToPath(new URL('../../backend/tests/vectors/guest_v1.json', import.meta.url)),
    'utf-8'
  )
);
const enc = new TextEncoder();
const dec = new TextDecoder();
const room: string = vec.room;
const key = keyFromFragment(vec.key_b64url);
let keys: GuestKeys;
beforeAll(async () => {
  keys = await deriveKeys(key);
});

describe('keys', () => {
  it('round-trips the fragment and derives the vector keys', () => {
    expect(key.length).toBe(32);
    expect(keyToFragment(key)).toBe(vec.key_b64url);
    expect(hex(keys.framesKeyBytes)).toBe(vec.frames_key_hex);
    expect(keys.roomToken).toBe(vec.room_token);
    expect(hex(keys.mediaNameKeyBytes)).toBe(vec.media_name_key_hex);
  });
  it('refuses malformed fragments', () => {
    for (const bad of ['', 'short', 'A'.repeat(44), 'A'.repeat(42), '!'.repeat(43), 'A'.repeat(42) + 'B']) {
      expect(() => keyFromFragment(bad)).toThrow(GuestFormatError);
    }
  });
  it('makes rooms and keys', () => {
    expect(newRoom()).toMatch(/^[0-9a-f]{16}$/);
    expect(newKey().length).toBe(32);
    expect(b64urlDecode(b64urlEncode(Uint8Array.from([0, 251, 255])))).toEqual(Uint8Array.from([0, 251, 255]));
  });
});

describe('vectors', () => {
  for (const [name, s] of Object.entries<any>(vec.samples)) {
    const nonce = fromHex(s.nonce_hex);
    const envelope = b64urlDecode(s.envelope_b64url);
    const plain = fromHex(s.plaintext_hex);

    it(`${name}: aad, size, and byte-for-byte envelope`, async () => {
      expect(dec.decode(aad(room, s.kind, s.sha256))).toBe(s.aad);
      expect(blockFor(s.kind)).toBe(s.padded_length);
      expect(await seal(keys, room, s.kind, plain, nonce, s.sha256)).toEqual(envelope);
      const opened = await unseal(keys, room, envelope, s.sha256);
      expect(opened.kind).toBe(s.kind);
      expect(opened.plaintext).toEqual(plain);
    });

    if (s.sha256 !== undefined) {
      it(`${name}: media name, seal and unseal`, async () => {
        expect(s.padded_length).toBe(MEDIA_BLOCK);
        expect(plain.length).toBe(1000);
        expect(await mediaName(keys, s.sha256)).toBe(s.name);
        expect(await sealMedia(keys, room, s.sha256, plain, nonce)).toEqual(envelope);
        expect(await unsealMedia(keys, room, s.sha256, envelope)).toEqual(plain);
      });
    } else if (s.frame_hex !== undefined) {
      it(`${name}: notes plaintext, seal and unseal`, async () => {
        const frame = fromHex(s.frame_hex);
        expect(notesPlaintext(s.gid, s.name, frame)).toEqual(plain);
        expect(await sealNotes(keys, room, s.gid, s.name, frame, nonce)).toEqual(envelope);
        expect(await unsealNotes(keys, room, envelope)).toEqual({ gid: s.gid, name: s.name, frame });
      });
    } else {
      it(`${name}: canonical JSON, seal and unseal`, async () => {
        const obj = JSON.parse(s.json);
        expect(dec.decode(canonicalJson(obj))).toBe(s.json);
        expect(await sealJson(keys, room, s.kind, obj, nonce)).toEqual(envelope);
        expect(await unsealJson(keys, room, envelope)).toEqual({ kind: s.kind, obj });
      });
    }
  }
});

describe('errors', () => {
  const s = vec.samples.notes;
  const envelope = b64urlDecode(s.envelope_b64url);
  const bad = async (p: Promise<unknown>) => expect(p).rejects.toBeInstanceOf(GuestFormatError);

  it('refuses a wrong key, room, kind byte and tampering', async () => {
    await bad(unseal(await deriveKeys(newKey()), room, envelope));
    await bad(unseal(keys, '0123456789abcdef', envelope));
    const kind = envelope.slice();
    kind[0] = Kind.COMMENT;
    await bad(unseal(keys, room, kind));
    const flipped = envelope.slice();
    flipped[40] ^= 1;
    await bad(unseal(keys, room, flipped));
  });
  it('refuses short data and unknown kinds', async () => {
    await bad(unseal(keys, room, envelope.slice(0, 28)));
    await bad(unseal(keys, room, envelope.slice(0, 100)));
    for (const k of [0, 7, 255]) {
      const e = envelope.slice();
      e[0] = k;
      await bad(unseal(keys, room, e));
    }
    await bad(seal(keys, room, 9, new Uint8Array(1)));
    await bad(seal(keys, room, 7, new Uint8Array(1)));
    await bad(unseal(keys, 'NOT-A-ROOM', envelope));
  });
  it('refuses bad padding even when it decrypts', async () => {
    const dirty = new Uint8Array(256);
    new DataView(dirty.buffer).setUint32(0, 3);
    dirty[255] = 1;
    // Seal the padded bytes as-is by hand through the public pieces.
    const nonce = new Uint8Array(12);
    const ct = new Uint8Array(
      await crypto.subtle.encrypt(
        { name: 'AES-GCM', iv: nonce, additionalData: aad(room, Kind.META) as BufferSource, tagLength: 128 },
        keys.framesKey,
        dirty
      )
    );
    const env = new Uint8Array(1 + 12 + ct.length);
    env[0] = Kind.META;
    env.set(ct, 13);
    await bad(unseal(keys, room, env));
    expect(() => unpad(new Uint8Array(100), 256)).toThrow(GuestFormatError);
    expect(() => unpad(new Uint8Array(512), 256)).toThrow(/bad length/);
  });
  it('sealJson refuses NOTES and unsealJson refuses notes frames', async () => {
    await bad(sealJson(keys, room, Kind.NOTES as never, {}));
    await bad(unsealJson(keys, room, envelope));
    await bad(unsealNotes(keys, room, await sealJson(keys, room, Kind.META, { a: 1 })));
  });
});

describe('media', () => {
  const image = Uint8Array.from({ length: 5000 }, (_, i) => (i * 31 + 7) % 256);
  const sha = async (d: Uint8Array) =>
    hex(new Uint8Array(await crypto.subtle.digest('SHA-256', d as BufferSource)));
  const bad = async (p: Promise<unknown>) => expect(p).rejects.toBeInstanceOf(GuestFormatError);

  it('round-trips and pads to one 64 KiB block', async () => {
    const s = await sha(image);
    const env = await sealMedia(keys, room, s, image);
    expect(env[0]).toBe(Kind.MEDIA);
    expect(env.length).toBe(1 + 12 + 65536 + 16);
    expect(await unsealMedia(keys, room, s, env)).toEqual(image);
    const big = new Uint8Array(70_000).fill(1);
    expect((await sealMedia(keys, room, await sha(big), big)).length).toBe(env.length + 65536);
  });
  it('refuses a blob over the limit and a sha that does not match', async () => {
    const atLimit = new Uint8Array(MEDIA_MAX).fill(7);
    const env = await sealMedia(keys, room, await sha(atLimit), atLimit);
    expect(await unsealMedia(keys, room, await sha(atLimit), env)).toEqual(atLimit);
    const over = new Uint8Array(MEDIA_MAX + 1).fill(7);
    await bad(sealMedia(keys, room, await sha(over), over));
    await bad(sealMedia(keys, room, await sha(enc.encode('other')), image));
  });
  it('requires the sha for MEDIA only', async () => {
    const s = await sha(image);
    expect(() => aad(room, Kind.MEDIA)).toThrow(GuestFormatError);
    expect(() => aad(room, Kind.NOTES, s)).toThrow(GuestFormatError);
    expect(dec.decode(aad(room, Kind.MEDIA, s))).toBe(`mnemosyne/guest/1|${room}|6|${s}`);
    await bad(seal(keys, room, Kind.MEDIA, image));
    await bad(seal(keys, room, Kind.NOTES, image, undefined, s));
    for (const wrong of ['', 'abc', s.toUpperCase(), s.slice(1), s + '0', 'g'.repeat(64)]) {
      expect(() => aad(room, Kind.MEDIA, wrong)).toThrow(GuestFormatError);
      await bad(mediaName(keys, wrong));
    }
  });
  it('refuses a wrong sha, room, key, kind, tampering and lying plaintext', async () => {
    const s = await sha(image);
    const other = await sha(enc.encode('other'));
    const env = await sealMedia(keys, room, s, image);
    await bad(unsealMedia(keys, room, other, env));
    await bad(unseal(keys, room, env));
    await bad(unsealMedia(keys, '0123456789abcdef', s, env));
    await bad(unsealMedia(await deriveKeys(newKey()), room, s, env));
    await bad(unsealMedia(keys, room, s, new Uint8Array(0)));
    const asNotes = env.slice();
    asNotes[0] = Kind.NOTES;
    await bad(unsealMedia(keys, room, s, asNotes));
    const flipped = env.slice();
    flipped[100] ^= 1;
    await bad(unsealMedia(keys, room, s, flipped));
    const liar = await seal(keys, room, Kind.MEDIA, enc.encode('not the image'), undefined, s);
    await bad(unsealMedia(keys, room, s, liar));
    await bad(unsealNotes(keys, room, env));
    await bad(sealJson(keys, room, Kind.MEDIA as never, {}));
    await bad(unsealMedia(keys, room, s, await sealNotes(keys, room, '', '', Uint8Array.from([0xde]))));
  });
  it('names a blob by an HMAC of its sha, per key', async () => {
    const s = await sha(image);
    const name = await mediaName(keys, s);
    expect(name).toMatch(/^[0-9a-f]{64}$/);
    expect(name).not.toBe(s);
    expect(await mediaName(keys, s)).toBe(name);
    expect(await mediaName(await deriveKeys(newKey()), s)).not.toBe(name);
    expect(await mediaName(keys, await sha(enc.encode('other')))).not.toBe(name);
    expect(name).not.toBe(await sha(await sealMedia(keys, room, s, image)));
  });
});

describe('notes fields', () => {
  const frame = Uint8Array.from([0xde, 1, 2]);
  it('truncates the name at a character boundary', () => {
    const name = 'é'.repeat(200); // 400 bytes
    const out = parseNotes(notesPlaintext('g', name, frame));
    expect(out.name).toBe('é'.repeat(127));
    expect(enc.encode(out.name).length).toBe(254);
    const emoji = parseNotes(notesPlaintext('g', 'a' + '🎉'.repeat(80), frame)).name;
    expect(emoji).toBe('a' + '🎉'.repeat(63)); // 1 + 252 bytes; the next emoji would reach 257
    expect(parseNotes(notesPlaintext('g', 'x'.repeat(255), frame)).name.length).toBe(255);
  });
  it('refuses a long gid and bad first bytes, both ways', () => {
    expect(() => notesPlaintext('g'.repeat(256), 'n', frame)).toThrow(GuestFormatError);
    expect(() => notesPlaintext('g', 'n', new Uint8Array(0))).toThrow(GuestFormatError);
    expect(() => notesPlaintext('g', 'n', Uint8Array.from([0x00, 1]))).toThrow(GuestFormatError);
    expect(() => notesPlaintext('g', 'n', Uint8Array.from([0xaf]))).not.toThrow();
    expect(() => parseNotes(Uint8Array.from([0, 0]))).toThrow(GuestFormatError);
    expect(() => parseNotes(Uint8Array.from([0, 0, 0x41]))).toThrow(GuestFormatError);
    expect(() => parseNotes(Uint8Array.from([5, 65]))).toThrow(/truncated/);
    expect(() => parseNotes(Uint8Array.from([0, 1, 0xff, 0xde]))).toThrow(/bad name/);
  });
});

describe('canonical JSON', () => {
  it('sorts recursively, is compact and keeps non-ASCII', () => {
    expect(dec.decode(canonicalJson({ b: [1, 2.5, { z: null, a: true }], a: 'é🎉"\n' }))).toBe(
      '{"a":"é🎉\\"\\n","b":[1,2.5,{"a":true,"z":null}]}'
    );
  });
  it('orders keys by code point like Python', () => {
    expect(dec.decode(canonicalJson({ '🎉': 1, '￮': 2 }))).toBe('{"￮":2,"🎉":1}');
  });
});
