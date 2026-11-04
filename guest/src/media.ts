// The guest page's images (task 8 of the images plan): the notes editor's MediaProvider on the room.
//
// - upload: the prepared bytes are sealed (format.ts `sealMedia`, bound to their sha) and stored in the
//   room under `mediaName(sha)`, a name the host cannot read but every holder of the link computes.
//   The image shows from our own bytes at once; the state is `uploading` until the room answers,
//   `failed` when it refused (the editor keeps the bytes and calls upload again for Retry).
// - url: an object URL. The bytes come from the copy kept in this browser (copy.ts `media` store), else
//   from the room (`getMedia`), are opened with `unsealMedia` (the AAD and the hash must both match the
//   sha the line names, so a wrong blob under a name shows as missing) and then kept in the copy.
//   Only WebP and JPEG ever become a URL, whatever a peer sealed.
// - missing: the room does not have the blob yet (its owner is still uploading, or is away). We ask
//   again when the host announces a blob (`heard`) and on a timer, 10, 30 and 60 s after the failed
//   asks, then give up with state `missing` ("Image not available") until a broadcast says it arrived.
//
// Nothing here throws to the editor: `url` answers null and tells `subscribe` when that changes.
import type { MediaProvider, PreparedImage } from '$lib/notes/ui/types.js';
import type { CopyStore } from './copy';
import { mediaName, sealMedia, unsealMedia, type GuestKeys } from './format';

/** Waits between the failed asks for a blob the room lacks; after the last one it counts as missing (ms). */
export const MISSING_RETRY_MS = [10_000, 30_000, 60_000] as const;

const SHA = /^[0-9a-f]{64}$/;

/** The part of RoomClient the images use. */
export interface MediaRoom {
  putMedia(name: string, env: Uint8Array): Promise<void>;
  getMedia(name: string): Promise<Uint8Array>;
}

export interface UrlMaker {
  create(bytes: Uint8Array, mime: string): string;
  revoke(url: string): void;
}

const browserUrls: UrlMaker = {
  create: (bytes, mime) => URL.createObjectURL(new Blob([bytes as BlobPart], { type: mime })),
  revoke: (url) => URL.revokeObjectURL(url)
};

export interface GuestMediaOptions {
  room: string;
  keys: GuestKeys;
  transport: MediaRoom;
  copies: CopyStore;
  /** May images be kept in the saved copy? (The room's expiry is known and the room is not gone.) */
  keep?: () => boolean;
  urls?: UrlMaker;
}

/** `webp` / `jpeg` by the bytes' magic number, else null (SVG, GIF and the rest never get a URL). */
export function sniff(b: Uint8Array): 'webp' | 'jpeg' | null {
  if (b.length >= 12 && b[0] === 0x52 && b[1] === 0x49 && b[2] === 0x46 && b[3] === 0x46) {
    if (b[8] === 0x57 && b[9] === 0x45 && b[10] === 0x42 && b[11] === 0x50) return 'webp';
  }
  if (b.length >= 3 && b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return 'jpeg';
  return null;
}

interface Waiting {
  name: string;
  /** Failed asks so far. */
  tries: number;
  timer: ReturnType<typeof setTimeout> | null;
}

export class GuestMedia implements MediaProvider {
  private readonly urls: UrlMaker;
  private objectUrls = new Map<string, string>();
  private uploads = new Map<string, 'uploading' | 'failed'>();
  private running = new Map<string, Promise<void>>();
  /** Shas being fetched. */
  private loading = new Set<string>();
  /** Shas the room did not have: waiting for a retry. */
  private waiting = new Map<string, Waiting>();
  /** Shas given up on until a broadcast. */
  private gaveUp = new Set<string>();
  /** sha -> the room's name for it, once an ask was made. */
  private names = new Map<string, string>();
  /** Shas the room announced while an ask was in flight. */
  private again = new Set<string>();
  private listeners = new Set<(sha: string) => void>();
  private disposed = false;

  constructor(private readonly o: GuestMediaOptions) {
    this.urls = o.urls ?? browserUrls;
  }

  // ---- MediaProvider ---------------------------------------------------------------------------

  url(sha: string): string | null {
    const have = this.objectUrls.get(sha);
    if (have) return have;
    if (!SHA.test(sha) || this.disposed) return null;
    if (!this.loading.has(sha) && !this.waiting.has(sha) && !this.gaveUp.has(sha)) void this.load(sha);
    return null;
  }

  state(sha: string): 'uploading' | 'failed' | 'missing' | null {
    return this.uploads.get(sha) ?? (this.gaveUp.has(sha) ? 'missing' : null);
  }

  subscribe(fn: (sha: string) => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  /** Seal and store the image; the editor shows it from our bytes meanwhile. Rejects when the room refused. */
  upload(image: PreparedImage): Promise<void> {
    const { sha } = image;
    const running = this.running.get(sha);
    if (running) return running;
    this.uploads.set(sha, 'uploading');
    this.gaveUp.delete(sha);
    if (!this.objectUrls.has(sha)) this.objectUrls.set(sha, this.urls.create(image.bytes, `image/${image.ext}`));
    this.notify(sha);
    const job = (async () => {
      try {
        const env = await sealMedia(this.o.keys, this.o.room, sha, image.bytes);
        const name = await mediaName(this.o.keys, sha);
        await this.o.transport.putMedia(name, env);
        this.uploads.delete(sha);
        this.keep(name, env);
      } catch (e) {
        this.uploads.set(sha, 'failed');
        throw e;
      } finally {
        this.running.delete(sha);
        this.notify(sha);
      }
    })();
    this.running.set(sha, job);
    return job;
  }

  // ---- the room's word -------------------------------------------------------------------------

  /** The host says the blob called `name` landed: a sha waiting for it asks at once. */
  heard(name: string): void {
    for (const [sha, n] of this.names) {
      if (n !== name || this.objectUrls.has(sha)) continue;
      if (this.loading.has(sha)) {
        this.again.add(sha); // the ask in flight may have been answered before the blob landed
        continue;
      }
      const w = this.waiting.get(sha);
      if (w?.timer) clearTimeout(w.timer);
      const was = this.gaveUp.delete(sha);
      this.waiting.delete(sha);
      if (was) this.notify(sha);
      void this.load(sha);
    }
  }

  /** Revoke the URLs and stop the timers. */
  dispose(): void {
    this.disposed = true;
    for (const w of this.waiting.values()) if (w.timer) clearTimeout(w.timer);
    this.waiting.clear();
    for (const u of this.objectUrls.values()) this.urls.revoke(u);
    this.objectUrls.clear();
    this.listeners.clear();
  }

  // ---- internals -------------------------------------------------------------------------------

  private notify(sha: string): void {
    for (const fn of [...this.listeners]) {
      try {
        fn(sha);
      } catch {
        /* a listener's problem */
      }
    }
  }

  private keep(name: string, env: Uint8Array): void {
    if (this.o.keep && !this.o.keep()) return;
    this.o.copies.putMedia(this.o.room, name, env).catch(() => {});
  }

  private async load(sha: string): Promise<void> {
    if (this.loading.has(sha)) return;
    this.loading.add(sha);
    try {
      const name = await mediaName(this.o.keys, sha);
      this.names.set(sha, name);
      let bytes = await this.fromCopy(sha, name);
      if (!bytes) {
        let env: Uint8Array;
        try {
          env = await this.o.transport.getMedia(name);
        } catch {
          this.later(sha, name);
          return;
        }
        try {
          bytes = await unsealMedia(this.o.keys, this.o.room, sha, env);
        } catch {
          // Not the image this line names (altered, or sealed for something else): as if absent.
          this.later(sha, name);
          return;
        }
        this.keep(name, env);
      }
      const ext = sniff(bytes);
      if (!ext) {
        this.later(sha, name);
        return;
      }
      if (this.disposed) return;
      this.waiting.delete(sha);
      this.gaveUp.delete(sha);
      this.objectUrls.set(sha, this.urls.create(bytes, `image/${ext}`));
      this.notify(sha);
    } finally {
      this.loading.delete(sha);
    }
  }

  private async fromCopy(sha: string, name: string): Promise<Uint8Array | null> {
    try {
      const env = await this.o.copies.getMedia(this.o.room, name);
      return env ? await unsealMedia(this.o.keys, this.o.room, sha, env) : null;
    } catch {
      return null; // an unreadable copy is asked for again
    }
  }

  /** The room has no usable blob under `name`: ask again after the next wait, or give up. */
  private later(sha: string, name: string): void {
    if (this.disposed) return;
    const prev = this.waiting.get(sha);
    if (prev?.timer) clearTimeout(prev.timer);
    if (this.again.delete(sha)) {
      this.waiting.delete(sha);
      queueMicrotask(() => void this.load(sha)); // after this ask has finished
      return;
    }
    const tries = prev ? prev.tries + 1 : 0;
    if (tries >= MISSING_RETRY_MS.length) {
      this.waiting.delete(sha);
      this.gaveUp.add(sha);
      this.notify(sha);
      return;
    }
    const w: Waiting = { name, tries, timer: null };
    w.timer = setTimeout(() => {
      w.timer = null;
      void this.load(sha); // stays in `waiting` meanwhile, so the count of tries is kept
    }, MISSING_RETRY_MS[tries]);
    this.waiting.set(sha, w);
  }
}
