// @vitest-environment node
// Node has crypto.subtle globally (jsdom, the default here, does not).
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
  MANIFEST_BLOCK,
  PublicationFormatError,
  aad,
  b64urlDecode,
  b64urlEncode,
  blobHash,
  decodeKey,
  deriveKey,
  fromHex,
  hex,
  openBlob,
  openClip,
  openManifest,
  unpad
} from './format';

const vec = JSON.parse(
  readFileSync(
    fileURLToPath(new URL('../../backend/tests/vectors/publication_v1.json', import.meta.url)),
    'utf-8'
  )
);
const key = decodeKey(vec.key_b64url);
const enc = new TextEncoder();
const dec = new TextDecoder();

describe('keys and encodings', () => {
  it('decodes the link key', () => {
    expect(key.length).toBe(32);
    expect(b64urlEncode(key)).toBe(vec.key_b64url);
  });
  it('refuses malformed keys', () => {
    for (const bad of ['', 'short', 'A'.repeat(44), 'A'.repeat(42), '!'.repeat(43), 'A'.repeat(42) + 'B']) {
      expect(() => decodeKey(bad)).toThrow(PublicationFormatError);
    }
  });
  it('round-trips base64url and hex', () => {
    const data = Uint8Array.from([0, 250, 251, 252, 253, 254, 255, 1]);
    expect(b64urlDecode(b64urlEncode(data))).toEqual(data);
    expect(fromHex(hex(data))).toEqual(data);
  });
});

describe('padding', () => {
  it('strips exactly', () => {
    const body = enc.encode('héllo');
    const padded = new Uint8Array(MANIFEST_BLOCK);
    new DataView(padded.buffer).setUint32(0, body.length);
    padded.set(body, 4);
    expect(unpad(padded, MANIFEST_BLOCK)).toEqual(body);
    const dirty = padded.slice();
    dirty[MANIFEST_BLOCK - 1] = 1;
    expect(() => unpad(dirty, MANIFEST_BLOCK)).toThrow(/zeros/);
    expect(() => unpad(padded.slice(0, 100), MANIFEST_BLOCK)).toThrow(PublicationFormatError);
    expect(() => unpad(new Uint8Array(2 * MANIFEST_BLOCK), MANIFEST_BLOCK)).toThrow(/bad length/);
  });
});

describe('vectors', () => {
  const m = vec.manifest;
  const blob = b64urlDecode(m.blob_b64url);

  it('has the AAD the format specifies', () => {
    expect(dec.decode(aad(vec.id, 'manifest', m.version))).toBe(m.aad);
    expect(dec.decode(aad(vec.id, 'clip', vec.clip.first_version))).toBe(vec.clip.aad);
  });

  it('hashes blobs to their address', async () => {
    expect(await blobHash(blob)).toBe(m.sha256);
  });

  it('opens the manifest', async () => {
    const plain = await openBlob('manifest', vec.id, m.version, blob, key);
    expect(dec.decode(plain)).toBe(m.json);
    expect(hex(plain)).toBe(m.json_hex);
    const manifest = await openManifest(vec.id, m.version, blob, key);
    expect(manifest.title).toContain('🚀');
    expect(manifest.attendees).toContain('李雷');
    expect(manifest.quotes[0].clip?.v).toBe(vec.clip.first_version);
    expect(manifest).not.toHaveProperty('segments');
  });

  it('opens the clip with the version that first uploaded it', async () => {
    const manifest = await openManifest(vec.id, m.version, blob, key);
    const ref = manifest.quotes[0].clip!;
    const clip = await openClip(vec.id, ref, b64urlDecode(vec.clip.blob_b64url), key);
    expect(hex(clip)).toBe(vec.clip.plaintext_hex);
    // Using the manifest's own version (2) instead of the clip's (1) must fail.
    await expect(
      openBlob('clip', vec.id, manifest.version, b64urlDecode(vec.clip.blob_b64url), key)
    ).rejects.toThrow(/Could not decrypt/);
    await expect(openClip(vec.id, { ...ref, sha256: '0'.repeat(64) }, b64urlDecode(vec.clip.blob_b64url), key)).rejects.toThrow(/address/);
  });

  it('fails on a wrong key, id, kind, version or altered data', async () => {
    const wrong = key.slice();
    wrong[0] ^= 1;
    await expect(openBlob('manifest', vec.id, m.version, blob, wrong)).rejects.toThrow(/Could not decrypt/);
    await expect(openBlob('manifest', vec.id, m.version + 1, blob, key)).rejects.toThrow(/Could not decrypt/);
    await expect(openBlob('clip', vec.id, m.version, blob, key)).rejects.toThrow(/Could not decrypt/);
    await expect(openBlob('manifest', 'fedcba9876543210', m.version, blob, key)).rejects.toThrow(/Could not decrypt/);
    const flipped = blob.slice();
    flipped[30] ^= 1;
    await expect(openBlob('manifest', vec.id, m.version, flipped, key)).rejects.toThrow(/Could not decrypt/);
    await expect(openBlob('manifest', vec.id, m.version, blob.slice(0, 20), key)).rejects.toThrow(/too short/);
  });

  it('derives the password key and opens the password copy', async () => {
    const p = vec.password;
    const derived = await deriveKey(p.password, fromHex(p.salt_hex), p.iterations);
    expect(b64urlEncode(derived)).toBe(p.key_b64url);
    const manifest = await openManifest(
      vec.id,
      m.version,
      b64urlDecode(p.manifest_blob_b64url),
      derived
    );
    expect(manifest.id).toBe(vec.id);
    const wrong = await deriveKey('not it', fromHex(p.salt_hex), p.iterations);
    await expect(
      openManifest(vec.id, m.version, b64urlDecode(p.manifest_blob_b64url), wrong)
    ).rejects.toThrow(/Could not decrypt/);
    await expect(deriveKey('x', fromHex(p.salt_hex), 1000)).rejects.toThrow(/out of range/);
  });
});
