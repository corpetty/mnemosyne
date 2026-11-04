// Image lines (docs/plans/2026-10-26-images-in-notes.md, tasks in 2026-11-04-images-tasks.md):
// a line of kind `image` whose text is the caption, the picture named by a closed `image` mark
// on its "\n". The mark's value is `<sha256 hex>.<webp|jpeg>@<w>x<h>`: the stored bytes' hash
// and type and the image's size in pixels, so a reader can draw a box of the right size before
// the bytes arrive. The same pattern is checked by notes-core (`IMAGE_VALUE`, model.rs) and the
// backend (services/notes_media.py); anything else on an `image` mark is no image. Markdown
// writes the line as `![caption](mnemosyne:image/<value>)`. No DOM here.

/** The mark kind on an image line's terminator. */
export const IMAGE = 'image';

/** The form of an `image` mark's value (notes-core's `IMAGE_VALUE`). */
export const IMAGE_VALUE = /^([0-9a-f]{64})\.(webp|jpeg)@([1-9][0-9]{0,4})x([1-9][0-9]{0,4})$/;

/** What an image line's markdown destination starts with; the mark value follows. */
export const IMAGE_URL = 'mnemosyne:image/';

export type ImageExt = 'webp' | 'jpeg';

export interface ImageRef {
  /** The whole mark value. */
  value: string;
  /** Hex SHA-256 of the stored bytes. */
  sha: string;
  ext: ImageExt;
  /** Size in pixels. */
  w: number;
  h: number;
}

/** Does `v` match the image value pattern? */
export function isImageValue(v: unknown): v is string {
  return typeof v === 'string' && IMAGE_VALUE.test(v);
}

/** The mark value for an image; throws when the parts cannot make a valid one. */
export function imageValue(img: { sha: string; ext: ImageExt; w: number; h: number }): string {
  const v = `${img.sha}.${img.ext}@${img.w}x${img.h}`;
  if (!isImageValue(v)) throw new RangeError(`not an image value: ${v}`);
  return v;
}

/** The parts of a mark value, or null for anything that does not match the pattern exactly. */
export function parseImageValue(v: string): ImageRef | null {
  if (typeof v !== 'string') return null;
  const m = IMAGE_VALUE.exec(v);
  if (!m) return null;
  return { value: v, sha: m[1], ext: m[2] as ImageExt, w: Number(m[3]), h: Number(m[4]) };
}

/** The markdown destination of an image line (`mnemosyne:image/<value>`). */
export function imageUrl(v: string): string {
  return IMAGE_URL + v;
}

/** The image a `mnemosyne:image/<value>` URL names, or null. */
export function parseImageUrl(url: string): ImageRef | null {
  if (typeof url !== 'string' || !url.startsWith(IMAGE_URL)) return null;
  return parseImageValue(url.slice(IMAGE_URL.length));
}
