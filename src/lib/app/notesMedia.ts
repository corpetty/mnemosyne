// The desktop's MediaProvider for the notes editor (images in the notes): the bytes of an image
// line come from `GET /api/sessions/{id}/notes/media/{sha}` over the connection (an <img> cannot
// carry the bearer token) and are shown from object URLs; a pasted image goes up with the PUT.
//
// A 404 means the blob is not on this computer yet (another machine just added the line): the
// fetch is tried again on the `notes_media` bus event for that sha (`event`) and on a timer at
// 10 s, 30 s and 60 s, then the sha is `missing` until a new event arrives.
import { getNotesMedia, putNotesMedia } from '$lib/api/backend.js';
import type { MediaProvider, PreparedImage } from '$lib/notes/ui/types.js';

/** Seconds to wait before each further try after a 404. */
export const RETRY_SECONDS = [10, 30, 60];

export interface NotesMediaApi {
  get(sessionId: string, sha: string): Promise<Blob | null>;
  put(sessionId: string, sha: string, bytes: Uint8Array, contentType: string): Promise<void>;
}

export interface NotesMedia extends MediaProvider {
  /** The `notes_media` bus event for this meeting: the blob is stored here now. */
  event(sha: string): void;
  /** Revoke the object URLs and stop the timers (the component's unmount). */
  dispose(): void;
}

export function createNotesMedia(
  sessionId: string,
  api: NotesMediaApi = { get: getNotesMedia, put: putNotesMedia }
): NotesMedia {
  const urls = new Map<string, string>();
  const states = new Map<string, 'uploading' | 'failed' | 'missing'>();
  const fetching = new Set<string>();
  /** An event arrived while a fetch was in flight: go again once it answers. */
  const again = new Set<string>();
  const attempts = new Map<string, number>();
  const timers = new Map<string, ReturnType<typeof setTimeout>>();
  const subs = new Set<(sha: string) => void>();
  let disposed = false;

  const notify = (sha: string) => {
    for (const fn of [...subs]) fn(sha);
  };

  function stored(sha: string, blob: Blob) {
    const old = urls.get(sha);
    if (old) URL.revokeObjectURL(old);
    urls.set(sha, URL.createObjectURL(blob));
    states.delete(sha);
    attempts.delete(sha);
    clearTimeout(timers.get(sha));
    timers.delete(sha);
  }

  async function load(sha: string) {
    if (disposed || urls.has(sha) || fetching.has(sha)) {
      if (fetching.has(sha)) again.add(sha);
      return;
    }
    fetching.add(sha);
    let blob: Blob | null = null;
    try {
      blob = await api.get(sessionId, sha);
    } catch {
      blob = null; // locked, offline: the same patience as a 404
    }
    fetching.delete(sha);
    if (disposed) return;
    if (blob) {
      if (!urls.has(sha)) stored(sha, blob);
      again.delete(sha);
      notify(sha);
      return;
    }
    if (again.delete(sha)) {
      attempts.delete(sha);
      void load(sha);
      return;
    }
    const n = attempts.get(sha) ?? 0;
    if (n >= RETRY_SECONDS.length) {
      states.set(sha, 'missing');
      notify(sha);
      return;
    }
    attempts.set(sha, n + 1);
    timers.set(
      sha,
      setTimeout(() => {
        timers.delete(sha);
        void load(sha);
      }, RETRY_SECONDS[n] * 1000)
    );
  }

  return {
    url(sha) {
      const have = urls.get(sha);
      if (have) return have;
      // Fetching, waiting for a retry, or given up (`missing`): nothing to start.
      if (!fetching.has(sha) && !timers.has(sha) && states.get(sha) !== 'missing') void load(sha);
      return null;
    },
    async upload(image: PreparedImage) {
      const { sha } = image;
      states.set(sha, 'uploading');
      notify(sha);
      try {
        await api.put(sessionId, sha, image.bytes, image.ext === 'webp' ? 'image/webp' : 'image/jpeg');
      } catch (e) {
        if (!disposed) {
          states.set(sha, 'failed');
          notify(sha);
        }
        throw e;
      }
      if (disposed) return;
      // The local bytes become the picture, so it never reloads from the server.
      if (!urls.has(sha)) stored(sha, new Blob([image.bytes as BlobPart], { type: image.ext === 'webp' ? 'image/webp' : 'image/jpeg' }));
      states.delete(sha);
      notify(sha);
    },
    subscribe(fn) {
      subs.add(fn);
      return () => subs.delete(fn);
    },
    state: (sha) => states.get(sha) ?? null,
    event(sha) {
      if (disposed || urls.has(sha)) return;
      const t = timers.get(sha);
      if (t) clearTimeout(t);
      timers.delete(sha);
      attempts.delete(sha);
      if (states.get(sha) === 'missing') states.delete(sha);
      void load(sha);
    },
    dispose() {
      disposed = true;
      for (const u of urls.values()) URL.revokeObjectURL(u);
      urls.clear();
      for (const t of timers.values()) clearTimeout(t);
      timers.clear();
      subs.clear();
    }
  };
}
