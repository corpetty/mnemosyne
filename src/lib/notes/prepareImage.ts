// An image file made ready for the notes (docs/plans/2026-10-26-images-in-notes.md, "Making
// one"): decoded, shrunk to at most MAX_EDGE pixels on the long edge, re-encoded as WebP (JPEG
// where the engine cannot encode WebP) and hashed. The re-encode on a canvas is what strips EXIF
// and GPS: only pixels survive. GIFs and SVGs come out as a still image (an SVG is never stored as
// SVG: scripts). The bytes, their type, the size and the hash are what the image line's mark
// names (images.ts `imageValue`) and what the media provider uploads.
import type { ImageExt } from './images.js';

/** The stored bytes' cap, decimal as said (the server's `IMAGE_MAX_BYTES`). */
export const IMAGE_MAX_BYTES = 2_000_000;
/** The long edge after shrinking, in pixels. */
export const MAX_EDGE = 1600;
export const WEBP_QUALITY = 0.82;
export const JPEG_QUALITY = 0.85;

export const TOO_LARGE = 'That image is too large even after shrinking';
export const UNREADABLE = 'That image format cannot be read here';

export interface PreparedImage {
  bytes: Uint8Array;
  ext: ImageExt;
  /** Size in pixels after shrinking. */
  w: number;
  h: number;
  /** Hex SHA-256 of `bytes`. */
  sha: string;
}

/** A file the notes will not take; `message` is for the person. */
export class ImageRefused extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ImageRefused';
  }
}

interface Decoded {
  source: CanvasImageSource;
  w: number;
  h: number;
  close(): void;
}

/** createImageBitmap with the file's orientation, else an <img> on an object URL (SVG, older engines). */
async function decode(file: Blob): Promise<Decoded> {
  if (typeof createImageBitmap === 'function') {
    try {
      const bmp = await createImageBitmap(file, { imageOrientation: 'from-image' });
      if (bmp.width > 0 && bmp.height > 0) return { source: bmp, w: bmp.width, h: bmp.height, close: () => bmp.close?.() };
      bmp.close?.();
    } catch {
      // fall through to the <img>
    }
  }
  if (typeof document === 'undefined' || typeof URL.createObjectURL !== 'function') throw new ImageRefused(UNREADABLE);
  const url = URL.createObjectURL(file);
  try {
    const img = document.createElement('img');
    await new Promise<void>((resolve, reject) => {
      img.onload = () => resolve();
      img.onerror = () => reject(new Error('decode'));
      img.src = url;
    });
    const w = img.naturalWidth;
    const h = img.naturalHeight;
    if (!(w > 0 && h > 0)) throw new Error('no size');
    return { source: img, w, h, close: () => URL.revokeObjectURL(url) };
  } catch {
    URL.revokeObjectURL(url);
    throw new ImageRefused(UNREADABLE);
  }
}

/** The size after shrinking to MAX_EDGE on the long edge (never enlarged). */
export function fitSize(w: number, h: number, edge = MAX_EDGE): { w: number; h: number } {
  const s = Math.min(1, edge / Math.max(w, h));
  return { w: Math.max(1, Math.round(w * s)), h: Math.max(1, Math.round(h * s)) };
}

const encode = (canvas: HTMLCanvasElement, type: string, quality: number): Promise<Blob | null> =>
  new Promise((resolve) => {
    try {
      canvas.toBlob((b) => resolve(b), type, quality);
    } catch {
      resolve(null);
    }
  });

async function bytesOf(blob: Blob): Promise<Uint8Array> {
  if (typeof blob.arrayBuffer === 'function') return new Uint8Array(await blob.arrayBuffer());
  return new Uint8Array(
    await new Promise<ArrayBuffer>((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(r.result as ArrayBuffer);
      r.onerror = () => reject(r.error);
      r.readAsArrayBuffer(blob);
    })
  );
}

/** Hex SHA-256. */
export async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', bytes as BufferSource);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('');
}

/**
 * Decode `file`, shrink, re-encode and hash it. Rejects with ImageRefused (its message is for the
 * person) for a file the engine cannot decode or encode and for one still over IMAGE_MAX_BYTES.
 */
export async function prepareImage(file: Blob): Promise<PreparedImage> {
  const d = await decode(file);
  let blob: Blob | null;
  let w: number;
  let h: number;
  try {
    ({ w, h } = fitSize(d.w, d.h));
    const canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new ImageRefused(UNREADABLE);
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(d.source, 0, 0, w, h);
    blob = await encode(canvas, 'image/webp', WEBP_QUALITY);
    if (!blob || blob.type !== 'image/webp') {
      // An engine without a WebP encoder hands back PNG (or nothing): JPEG then, which has no
      // alpha, so transparent parts go on white rather than black.
      ctx.globalCompositeOperation = 'destination-over';
      ctx.fillStyle = '#fff';
      ctx.fillRect(0, 0, w, h);
      blob = await encode(canvas, 'image/jpeg', JPEG_QUALITY);
      if (!blob || blob.type !== 'image/jpeg') throw new ImageRefused(UNREADABLE);
    }
  } catch (e) {
    throw e instanceof ImageRefused ? e : new ImageRefused(UNREADABLE);
  } finally {
    d.close();
  }
  const ext: ImageExt = blob.type === 'image/webp' ? 'webp' : 'jpeg';
  if (blob.size > IMAGE_MAX_BYTES) throw new ImageRefused(TOO_LARGE);
  const bytes = await bytesOf(blob);
  if (bytes.length > IMAGE_MAX_BYTES) throw new ImageRefused(TOO_LARGE);
  return { bytes, ext, w, h, sha: await sha256Hex(bytes) };
}
