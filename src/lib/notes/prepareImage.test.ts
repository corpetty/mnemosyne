// prepareImage with a stubbed decoder and canvas (jsdom has neither): the size it draws at, the
// WebP → JPEG fallback, the size refusal, the hash and an undecodable file.
import { createHash } from 'node:crypto';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { IMAGE_MAX_BYTES, ImageRefused, TOO_LARGE, UNREADABLE, fitSize, prepareImage } from './prepareImage.js';

const WEBP = [0x52, 0x49, 0x46, 0x46, 4, 0, 0, 0, 0x57, 0x45, 0x42, 0x50]; // RIFF....WEBP
const JPEG = [0xff, 0xd8, 0xff, 0xe0];

/** What the stubbed canvas encodes: `type` → bytes (null = the engine returns null). */
let encoders: Record<string, number[] | null>;
/** What toBlob was asked for, and what drawImage drew at. */
let asked: { type: string; quality: number }[];
let drawn: number[][];
let decoded: { w: number; h: number } | null;

function blobOf(bytes: number[], type: string): Blob {
  const b = new Blob([new Uint8Array(bytes)], { type });
  // jsdom's Blob may lack arrayBuffer in some versions: give it one.
  Object.defineProperty(b, 'arrayBuffer', { value: async () => new Uint8Array(bytes).buffer });
  return b;
}

beforeEach(() => {
  encoders = { 'image/webp': [...WEBP, 1, 2, 3] };
  asked = [];
  drawn = [];
  decoded = { w: 4000, h: 3000 };
  vi.stubGlobal('createImageBitmap', async () => {
    if (!decoded) throw new DOMException('The source image could not be decoded.', 'InvalidStateError');
    return { width: decoded.w, height: decoded.h, close() {} };
  });
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(function (this: HTMLCanvasElement) {
    return {
      drawImage: (_src: unknown, x: number, y: number, w: number, h: number) => drawn.push([x, y, w, h]),
      fillRect() {},
      set fillStyle(_v: string) {},
      set globalCompositeOperation(_v: string) {},
      set imageSmoothingEnabled(_v: boolean) {},
      set imageSmoothingQuality(_v: string) {}
    } as unknown as CanvasRenderingContext2D;
  } as never);
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(function (cb: BlobCallback, type?: string, quality?: number) {
    asked.push({ type: type ?? '', quality: quality ?? 0 });
    const t = type ?? 'image/png';
    const out = t in encoders ? encoders[t] : [0x89, 0x50, 0x4e, 0x47];
    // An engine without the encoder answers with PNG.
    setTimeout(() => cb(out === null ? null : blobOf(out, t in encoders ? t : 'image/png')), 0);
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const file = () => new Blob([new Uint8Array([1, 2, 3])], { type: 'image/png' });
const hex = (bytes: number[]) => createHash('sha256').update(Buffer.from(bytes)).digest('hex');

describe('prepareImage', () => {
  it('shrinks to 1600 px on the long edge, encodes WebP and hashes the bytes', async () => {
    const p = await prepareImage(file());
    expect(p).toMatchObject({ ext: 'webp', w: 1600, h: 1200 });
    expect(drawn).toEqual([[0, 0, 1600, 1200]]);
    expect(asked).toEqual([{ type: 'image/webp', quality: 0.82 }]);
    expect(Array.from(p.bytes.slice(0, 12))).toEqual(WEBP);
    expect(p.sha).toBe(hex([...WEBP, 1, 2, 3]));
    expect(p.sha).toMatch(/^[0-9a-f]{64}$/);
  });

  it('never enlarges', async () => {
    decoded = { w: 300, h: 900 };
    expect(await prepareImage(file())).toMatchObject({ w: 300, h: 900 });
    expect(fitSize(3200, 10)).toEqual({ w: 1600, h: 5 });
    expect(fitSize(1, 5000)).toEqual({ w: 1, h: 1600 });
  });

  it('falls back to JPEG when the engine hands back something other than WebP', async () => {
    delete encoders['image/webp']; // toBlob answers PNG for webp
    encoders['image/jpeg'] = [...JPEG, 9, 9];
    const p = await prepareImage(file());
    expect(p.ext).toBe('jpeg');
    expect(asked).toEqual([
      { type: 'image/webp', quality: 0.82 },
      { type: 'image/jpeg', quality: 0.85 }
    ]);
    expect(Array.from(p.bytes.slice(0, 4))).toEqual(JPEG);
    expect(p.sha).toBe(hex([...JPEG, 9, 9]));
  });

  it('falls back to JPEG when WebP encoding gives nothing', async () => {
    encoders['image/webp'] = null;
    encoders['image/jpeg'] = [...JPEG];
    expect((await prepareImage(file())).ext).toBe('jpeg');
  });

  it('refuses an image still over the cap after shrinking', async () => {
    encoders['image/webp'] = new Array(IMAGE_MAX_BYTES + 1).fill(7);
    const err = await prepareImage(file()).catch((e) => e);
    expect(err).toBeInstanceOf(ImageRefused);
    expect(err.message).toBe(TOO_LARGE);
    // Exactly at the cap is fine.
    encoders['image/webp'] = new Array(IMAGE_MAX_BYTES).fill(7);
    expect((await prepareImage(file())).bytes.length).toBe(IMAGE_MAX_BYTES);
  });

  it('refuses a file nothing can decode (HEIC here)', async () => {
    decoded = null;
    // The <img> fallback fails too.
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: () => 'blob:x', revokeObjectURL: () => {} }));
    const img = document.createElement('img');
    vi.spyOn(document, 'createElement').mockImplementation(((tag: string) => {
      if (tag !== 'img') throw new Error(`unexpected ${tag}`);
      queueMicrotask(() => img.onerror?.(new Event('error')));
      return img;
    }) as never);
    const err = await prepareImage(new Blob([new Uint8Array([0, 0, 0, 0x18, 0x66, 0x74, 0x79, 0x70])], { type: 'image/heic' })).catch((e) => e);
    expect(err).toBeInstanceOf(ImageRefused);
    expect(err.message).toBe(UNREADABLE);
    expect(drawn).toEqual([]);
  });

  it('refuses when neither WebP nor JPEG can be encoded', async () => {
    encoders['image/webp'] = null;
    encoders['image/jpeg'] = null;
    const err = await prepareImage(file()).catch((e) => e);
    expect(err.message).toBe(UNREADABLE);
  });
});
