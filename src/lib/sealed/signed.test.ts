// @vitest-environment node
// Node has crypto.subtle globally (jsdom, the default here, does not).
// Live-publication frames: round trips and refusals. The bytes are pinned against the Python side
// by publication_live_v1.json (task 9 of docs/plans/2026-11-05-live-publications.md).
import { beforeAll, describe, expect, it } from 'vitest';
import * as env from './envelope';
import {
  AAD_PREFIX,
  PUB_LIVE,
  PubKind,
  PubLiveFormatError,
  aad,
  boxAad,
  boxKey,
  boxOp,
  boxPlaintext,
  boxPublic,
  deriveLiveKeys,
  ed25519Sign,
  ed25519Verify,
  newPrivate,
  openBox,
  openSigned,
  parseLiveKeys,
  readerMessage,
  ridOf,
  seal,
  signFrame,
  signPublic,
  signedMessage,
  signedPlaintext,
  unseal,
  x25519Shared
} from './signed';

const enc = new TextEncoder();
const dec = new TextDecoder();
const room = 'a1b2c3d4e5f60718';
const key = Uint8Array.from({ length: 32 }, (_, i) => i + 0x40);

let keys: env.SpaceKeys;
const owner = { sign: newPrivate(), box: newPrivate() };
const ownerPub = { sign: signPublic(owner.sign), box: boxPublic(owner.box) };
const reader = newPrivate();
const other = newPrivate();

beforeAll(async () => {
  keys = await deriveLiveKeys(key);
});

/** A COMMENT envelope whose content is assembled by hand (for forgeries). */
async function handBox(content: Uint8Array, ownerBox: Uint8Array): Promise<Uint8Array> {
  const eph = newPrivate();
  const ephPub = boxPublic(eph);
  const aes = await env.aesKey(await boxKey(x25519Shared(eph, ownerBox), room));
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const ct = new Uint8Array(
    await crypto.subtle.encrypt(
      { name: 'AES-GCM', iv: env.buf(nonce), additionalData: env.buf(boxAad(room, ephPub)) },
      aes,
      env.buf(content)
    )
  );
  return seal(keys, room, PubKind.COMMENT, env.concat(ephPub, nonce, ct));
}

describe('the space', () => {
  it('derives its own keys from the link key, apart from the guest space', async () => {
    const guest = await env.deriveSpaceKeys(env.GUEST, key);
    expect(env.hex(keys.framesKeyBytes)).toBe(env.hex(await env.hkdf(key, 'mnemosyne/pub/frames')));
    expect(keys.roomToken).toBe(env.b64urlEncode(await env.hkdf(key, 'mnemosyne/pub/room')));
    expect(env.hex(keys.framesKeyBytes)).not.toBe(env.hex(guest.framesKeyBytes));
    expect(keys.mediaNameKey).toBeUndefined();
  });

  it('binds its prefix, room and kind into the AAD', () => {
    expect(dec.decode(aad(room, 3))).toBe(`mnemosyne/pub-live/1|${room}|3`);
    expect(() => aad(room, 5)).toThrow(PubLiveFormatError);
    expect(() => aad('A1B2C3D4E5F60718', 1)).toThrow(PubLiveFormatError);
  });

  it('pads every kind to 256 bytes', () => {
    for (const k of [1, 2, 3, 4]) expect(env.blockFor(PUB_LIVE, k)).toBe(256);
  });

  it('never opens a guest frame, nor a guest space frame as its own', async () => {
    const guest = await env.deriveSpaceKeys(env.GUEST, key);
    const g = await env.seal(env.GUEST, guest, room, 2, enc.encode('{}'));
    await expect(unseal(keys, room, g)).rejects.toBeInstanceOf(PubLiveFormatError);
    // Even under the same AES key the AAD prefix keeps them apart.
    const p = await seal(keys, room, 2, enc.encode('{}'));
    await expect(env.unseal(env.GUEST, keys, room, p)).rejects.toBeInstanceOf(env.SealedFormatError);
  });
});

describe('owner frames', () => {
  const version = { version: 5, sha256: 'ab'.repeat(32), published_at: '2026-11-05T10:15:00Z', expires_at: null };

  it('round-trips VERSION, THREADS and META', async () => {
    const threads = { at: '2026-11-05T10:22:31Z', threads: [{ root: { id: 'x', text: 'Zoë 🎉' }, replies: [] }] };
    const meta = { comments: true, follow: false, at: '2026-11-05T10:15:01Z' };
    for (const [kind, obj] of [
      [PubKind.VERSION, version],
      [PubKind.THREADS, threads],
      [PubKind.META, meta]
    ] as const) {
      const e = await signFrame(keys, room, kind, obj, owner.sign);
      expect((e.length - 29) % 256).toBe(0);
      const out = await openSigned(keys, room, e, ownerPub.sign);
      expect(out.kind).toBe(kind);
      expect(out.obj).toEqual(obj);
      await expect(openSigned(keys, room, e, ownerPub.sign, kind)).resolves.toBeTruthy();
    }
  });

  it('lays out sig(64) || canonical body, signed over prefix|room|kind| || body', () => {
    const plain = signedPlaintext(room, PubKind.VERSION, version, owner.sign);
    const body = plain.slice(64);
    expect(dec.decode(body)).toBe(dec.decode(env.canonicalJson(version)));
    const msg = signedMessage(room, PubKind.VERSION, body);
    expect(dec.decode(msg)).toBe(`${AAD_PREFIX}|${room}|1|${dec.decode(body)}`);
    expect(ed25519Verify(ownerPub.sign, plain.slice(0, 64), msg)).toBe(true);
    // Ed25519 is deterministic.
    expect(env.hex(ed25519Sign(owner.sign, msg))).toBe(env.hex(plain.slice(0, 64)));
  });

  it("drops a frame signed by a reader's key, an unsigned one and an altered body", async () => {
    const byReader = await signFrame(keys, room, PubKind.THREADS, { at: 'z', threads: [] }, reader);
    await expect(openSigned(keys, room, byReader, ownerPub.sign)).rejects.toThrow(/not signed by/);

    const unsigned = await seal(keys, room, PubKind.VERSION, env.canonicalJson(version));
    await expect(openSigned(keys, room, unsigned, ownerPub.sign)).rejects.toBeInstanceOf(PubLiveFormatError);
    await expect(openSigned(keys, room, await seal(keys, room, 1, new Uint8Array(10)), ownerPub.sign)).rejects.toThrow(
      /too short/
    );

    const plain = signedPlaintext(room, PubKind.META, { comments: true }, owner.sign);
    const forged = env.concat(plain.slice(0, 64), enc.encode('{"comments":false}'));
    await expect(openSigned(keys, room, await seal(keys, room, PubKind.META, forged), ownerPub.sign)).rejects.toThrow(
      /not signed by/
    );
  });

  it('binds the room and kind into the signature', async () => {
    // Re-sealed under another kind or room with the owner's own signature: refused.
    const plain = signedPlaintext(room, PubKind.META, { comments: false }, owner.sign);
    const asVersion = await seal(keys, room, PubKind.VERSION, plain);
    await expect(openSigned(keys, room, asVersion, ownerPub.sign)).rejects.toThrow(/not signed by/);
    const elsewhere = 'ffffffffffffffff';
    const moved = await seal(keys, elsewhere, PubKind.META, plain);
    await expect(openSigned(keys, elsewhere, moved, ownerPub.sign)).rejects.toThrow(/not signed by/);
  });

  it('refuses another kind than asked, COMMENT, and bodies that are not objects', async () => {
    const meta = await signFrame(keys, room, PubKind.META, { comments: true }, owner.sign);
    await expect(openSigned(keys, room, meta, ownerPub.sign, PubKind.VERSION)).rejects.toThrow(/expected kind/);
    const comment = await boxOp(keys, room, { op: 'create' }, reader, ownerPub.box);
    await expect(openSigned(keys, room, comment, ownerPub.sign)).rejects.toThrow(/Only VERSION/);
    expect(() => signedPlaintext(room, PubKind.COMMENT, {}, owner.sign)).toThrow(PubLiveFormatError);
    expect(() => signedPlaintext(room, PubKind.META, [1], owner.sign)).toThrow(/JSON object/);
    const arr = env.concat(ed25519Sign(owner.sign, signedMessage(room, 3, enc.encode('[1]'))), enc.encode('[1]'));
    await expect(openSigned(keys, room, await seal(keys, room, 3, arr), ownerPub.sign)).rejects.toThrow(/JSON object/);
  });

  it('verifies strictly and never throws from verify', () => {
    const msg = enc.encode('m');
    const sig = ed25519Sign(owner.sign, msg);
    expect(ed25519Verify(ownerPub.sign, sig, msg)).toBe(true);
    expect(ed25519Verify(ownerPub.sign, sig.slice(0, 63), msg)).toBe(false);
    expect(ed25519Verify(new Uint8Array(32), sig, msg)).toBe(false);
    expect(ed25519Verify(new Uint8Array(31), sig, msg)).toBe(false);
  });
});

describe('reader comments', () => {
  const op = {
    op: 'create',
    id: '0123456789ab',
    parent_id: null,
    text: 'Agreed, Zoë',
    name: 'Ana',
    at: '2026-11-05T10:20:00Z',
    anchor: { target: 'decision', target_id: 'd1', quote_id: null, exact: null, prefix: null, suffix: null }
  };

  it('boxes to the owner and opens with the reader id', async () => {
    const e = await boxOp(keys, room, op, reader, ownerPub.box);
    expect(e[0]).toBe(PubKind.COMMENT);
    expect((e.length - 29) % 256).toBe(0);
    const out = await openBox(keys, room, e, owner.box);
    expect(out.op).toEqual(op);
    expect(env.hex(out.readerPub)).toBe(env.hex(signPublic(reader)));
    expect(out.rid).toBe(await ridOf(signPublic(reader)));
    // A replayed frame opens again: idempotence is the bridge's.
    expect((await openBox(keys, room, e, owner.box)).op).toEqual(op);
  });

  it('lays out eph(32) || nonce(12) || ct, content sig(64) || readerPub(32) || op', async () => {
    const eph = newPrivate();
    const boxNonce = Uint8Array.from({ length: 12 }, (_, i) => i);
    const plain = await boxPlaintext(room, op, reader, ownerPub.box, { ephPrivate: eph, boxNonce });
    const ephPub = boxPublic(eph);
    expect(env.hex(plain.slice(0, 32))).toBe(env.hex(ephPub));
    expect(env.hex(plain.slice(32, 44))).toBe(env.hex(boxNonce));
    expect(dec.decode(boxAad(room, ephPub))).toBe(`${AAD_PREFIX}|${room}|4|${env.hex(ephPub)}`);
    const aes = await env.aesKey(await boxKey(x25519Shared(owner.box, ephPub), room));
    expect(env.hex(await boxKey(x25519Shared(owner.box, ephPub), room))).toBe(
      env.hex(await env.hkdf(x25519Shared(eph, ownerPub.box), 'mnemosyne/pub/box', enc.encode(room)))
    );
    const content = new Uint8Array(
      await crypto.subtle.decrypt(
        { name: 'AES-GCM', iv: env.buf(boxNonce), additionalData: env.buf(boxAad(room, ephPub)) },
        aes,
        env.buf(plain.slice(44))
      )
    );
    const readerPub = signPublic(reader);
    expect(env.hex(content.slice(64, 96))).toBe(env.hex(readerPub));
    const opBytes = content.slice(96);
    expect(dec.decode(opBytes)).toBe(dec.decode(env.canonicalJson(op)));
    expect(ed25519Verify(readerPub, content.slice(0, 64), readerMessage(room, readerPub, opBytes))).toBe(true);
    const head = `${AAD_PREFIX}|${room}|4|`;
    const msg = readerMessage(room, readerPub, opBytes);
    expect(dec.decode(msg.slice(0, head.length))).toBe(head);
    expect(env.hex(msg.slice(head.length, head.length + 32))).toBe(env.hex(readerPub));
  });

  it('does not open for another owner or with the reader keys', async () => {
    const e = await boxOp(keys, room, op, reader, ownerPub.box);
    await expect(openBox(keys, room, e, other)).rejects.toThrow(/not for this owner/);
    await expect(openBox(keys, room, e, reader)).rejects.toBeInstanceOf(PubLiveFormatError);
    await expect(openSigned(keys, room, e, signPublic(reader))).rejects.toBeInstanceOf(PubLiveFormatError);
  });

  it("refuses a signature that is not the carried key's", async () => {
    const opBytes = env.canonicalJson(op);
    const claimed = signPublic(other);
    // Signed by `reader` while claiming `other`'s key: someone editing under another rid.
    const sig = ed25519Sign(reader, readerMessage(room, claimed, opBytes));
    const forged = await handBox(env.concat(sig, claimed, opBytes), ownerPub.box);
    await expect(openBox(keys, room, forged, owner.box)).rejects.toThrow(/not signed by the key it carries/);
    // The honest hand-built frame opens (the forgery test above is not failing for another reason).
    const readerPub = signPublic(reader);
    const honest = env.concat(ed25519Sign(reader, readerMessage(room, readerPub, opBytes)), readerPub, opBytes);
    expect((await openBox(keys, room, await handBox(honest, ownerPub.box), owner.box)).op).toEqual(op);
  });

  it('refuses another room, an altered box and a too short one', async () => {
    const e = await boxOp(keys, room, op, reader, ownerPub.box);
    await expect(openBox(keys, 'ffffffffffffffff', e, owner.box)).rejects.toBeInstanceOf(PubLiveFormatError);
    // Same box moved into another room's envelope: the inner AAD and HKDF salt bind the room.
    const { plaintext } = await unseal(keys, room, e);
    const moved = await seal(keys, 'ffffffffffffffff', PubKind.COMMENT, plaintext);
    await expect(openBox(keys, 'ffffffffffffffff', moved, owner.box)).rejects.toThrow(/not for this owner/);
    const altered = plaintext.slice();
    altered[60] ^= 1;
    await expect(openBox(keys, room, await seal(keys, room, 4, altered), owner.box)).rejects.toThrow(
      /not for this owner/
    );
    await expect(openBox(keys, room, await seal(keys, room, 4, new Uint8Array(100)), owner.box)).rejects.toThrow(
      /too short/
    );
    const meta = await signFrame(keys, room, PubKind.META, {}, owner.sign);
    await expect(openBox(keys, room, meta, owner.box)).rejects.toThrow(/not a comment/);
  });

  it('refuses a low-order owner box key', async () => {
    await expect(boxOp(keys, room, op, reader, new Uint8Array(32))).rejects.toThrow(/cannot be used for a box/);
  });
});

describe('keys and ids', () => {
  it('derives rid as 16 hex of SHA-256 of the public key', async () => {
    const pub = signPublic(reader);
    expect(await ridOf(pub)).toBe((await env.sha256Hex(pub)).slice(0, 16));
    await expect(ridOf(new Uint8Array(31))).rejects.toBeInstanceOf(PubLiveFormatError);
  });

  it('checks key sizes', () => {
    expect(() => signPublic(new Uint8Array(31))).toThrow(/private key is 32 bytes/);
    expect(() => x25519Shared(owner.box, new Uint8Array(33))).toThrow(/public key is 32 bytes/);
  });

  it("reads a manifest's live keys", () => {
    const live = { sign: env.hex(ownerPub.sign), box: env.hex(ownerPub.box) };
    const parsed = parseLiveKeys(live)!;
    expect(env.hex(parsed.sign)).toBe(live.sign);
    expect(env.hex(parsed.box)).toBe(live.box);
    expect(parseLiveKeys(undefined)).toBeNull();
    expect(parseLiveKeys({ sign: live.sign })).toBeNull();
    expect(parseLiveKeys({ sign: live.sign.toUpperCase(), box: live.box })).toBeNull();
  });
});
