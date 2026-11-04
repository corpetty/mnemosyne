// The guest page's saved copy (phase 10, C15): the last sealed snapshot of a room, kept in this
// browser until the link expires, so a guest who already opened the notes can reread them while
// the owner's computer is off. Only what the host sent is stored, exactly as received: the
// envelope (opened again with the key from the link each time) and the times around it. Never the
// key, never plaintext, so not the title or the owner either (they are inside the envelope).
//
// Images of the notes (task 8 of the images plan, C20) are kept the same way in a second object
// store: the sealed blob exactly as the room sent it, under `<room>/<name>` (the name the host knows
// it by). They go with the copy: `remove(room)` deletes both, and an expired copy is removed.
//
// Every call is best effort: a private window, blocked or full storage turns the store into a
// no-op, it never throws.

export interface CopyRec {
  /** The latest sealed SNAPSHOT envelope, as the room sent it. */
  envelope: Uint8Array;
  /** When the link expires (ISO 8601). The copy is deleted after it. */
  expires: string;
  /** When the room ended (ISO 8601), null while it had not. */
  ended_at: string | null;
  /** When this copy was saved (ISO 8601). */
  saved_at: string;
}

/** A sealed image kept in this browser. */
export interface MediaRec {
  /** The MEDIA envelope, as the room sent it. */
  envelope: Uint8Array;
  /** When it was saved (ISO 8601). */
  saved_at: string;
}

export interface CopyStore {
  save(room: string, rec: CopyRec): Promise<void>;
  load(room: string): Promise<CopyRec | null>;
  /** Deletes the room's copy and every image kept with it. */
  remove(room: string): Promise<void>;
  /** Keep a sealed image (`name`: the room's 64 hex name for it). */
  putMedia(room: string, name: string, envelope: Uint8Array): Promise<void>;
  /** The sealed image kept under `name`, or null. */
  getMedia(room: string, name: string): Promise<Uint8Array | null>;
}

/** A stored value if it has the shape of a CopyRec (and only those fields), else null. */
export function validRec(v: unknown): CopyRec | null {
  if (!v || typeof v !== 'object') return null;
  const o = v as Record<string, unknown>;
  if (!(o.envelope instanceof Uint8Array) || o.envelope.length === 0) return null;
  if (typeof o.expires !== 'string' || typeof o.saved_at !== 'string') return null;
  if (o.ended_at !== null && typeof o.ended_at !== 'string') return null;
  return { envelope: o.envelope, expires: o.expires, ended_at: o.ended_at, saved_at: o.saved_at };
}

/** A stored value if it has the shape of a MediaRec, else null. */
export function validMediaRec(v: unknown): MediaRec | null {
  if (!v || typeof v !== 'object') return null;
  const o = v as Record<string, unknown>;
  if (!(o.envelope instanceof Uint8Array) || o.envelope.length === 0) return null;
  if (typeof o.saved_at !== 'string') return null;
  return { envelope: o.envelope, saved_at: o.saved_at };
}

/** Has the copy's link expired at `nowMs`? An unreadable time counts as expired. */
export function expired(rec: CopyRec, nowMs: number): boolean {
  const t = Date.parse(rec.expires);
  return !Number.isFinite(t) || t <= nowMs;
}

/** For tests (and anywhere without storage). */
export class MemoryCopyStore implements CopyStore {
  readonly copies = new Map<string, CopyRec>();
  /** Keyed `<room>/<name>`, like the IndexedDB store. */
  readonly media = new Map<string, MediaRec>();
  async save(room: string, rec: CopyRec): Promise<void> {
    this.copies.set(room, { ...rec, envelope: rec.envelope.slice() });
  }
  async load(room: string): Promise<CopyRec | null> {
    const r = this.copies.get(room);
    return r ? { ...r, envelope: r.envelope.slice() } : null;
  }
  async remove(room: string): Promise<void> {
    this.copies.delete(room);
    for (const k of [...this.media.keys()]) if (k.startsWith(`${room}/`)) this.media.delete(k);
  }
  async putMedia(room: string, name: string, envelope: Uint8Array): Promise<void> {
    this.media.set(`${room}/${name}`, { envelope: envelope.slice(), saved_at: new Date().toISOString() });
  }
  async getMedia(room: string, name: string): Promise<Uint8Array | null> {
    return this.media.get(`${room}/${name}`)?.envelope.slice() ?? null;
  }
}

export const DB_NAME = 'mnemosyne-guest';
export const STORE_NAME = 'copies';
export const MEDIA_STORE = 'media';
/** 1: `copies`. 2: `media` beside it (the upgrade keeps what `copies` holds). */
export const DB_VERSION = 2;

/** IndexedDB `mnemosyne-guest`: `copies`, one record per room id; `media`, one per image (`<room>/<name>`). */
export class IndexedDbCopyStore implements CopyStore {
  private opening: Promise<IDBDatabase | null> | null = null;

  private db(): Promise<IDBDatabase | null> {
    this.opening ??= new Promise<IDBDatabase | null>((resolve) => {
      try {
        const req = indexedDB.open(DB_NAME, DB_VERSION);
        req.onupgradeneeded = () => {
          try {
            const db = req.result;
            if (!db.objectStoreNames.contains(STORE_NAME)) db.createObjectStore(STORE_NAME);
            if (!db.objectStoreNames.contains(MEDIA_STORE)) db.createObjectStore(MEDIA_STORE);
          } catch {
            /* the error below */
          }
        };
        req.onsuccess = () => {
          const db = req.result;
          // A newer page version upgrading the database must not wait for this tab.
          db.onversionchange = () => db.close();
          resolve(db);
        };
        req.onerror = () => resolve(null);
        req.onblocked = () => resolve(null);
      } catch {
        resolve(null); // no indexedDB, or it throws (some private windows)
      }
    });
    return this.opening;
  }

  /** Run one request in a transaction; resolves with its result, or `fallback` on any failure. */
  private async run<T>(
    store: string,
    mode: IDBTransactionMode,
    fallback: T,
    make: (s: IDBObjectStore) => IDBRequest
  ): Promise<T> {
    try {
      const db = await this.db();
      if (!db) return fallback;
      return await new Promise<T>((resolve) => {
        try {
          const tx = db.transaction(store, mode);
          const req = make(tx.objectStore(store));
          tx.oncomplete = () => resolve(req.result as T);
          tx.onerror = tx.onabort = () => resolve(fallback);
        } catch {
          resolve(fallback);
        }
      });
    } catch {
      return fallback;
    }
  }

  async save(room: string, rec: CopyRec): Promise<void> {
    await this.run<unknown>(STORE_NAME, 'readwrite', undefined, (s) => s.put(rec, room));
  }
  async load(room: string): Promise<CopyRec | null> {
    return validRec(await this.run<unknown>(STORE_NAME, 'readonly', null, (s) => s.get(room)));
  }
  async remove(room: string): Promise<void> {
    // The copy and the room's images in one transaction (keys `<room>/` up to `<room>/\uffff`).
    try {
      const db = await this.db();
      if (!db) return;
      await new Promise<void>((resolve) => {
        try {
          const tx = db.transaction([STORE_NAME, MEDIA_STORE], 'readwrite');
          tx.objectStore(STORE_NAME).delete(room);
          tx.objectStore(MEDIA_STORE).delete(IDBKeyRange.bound(`${room}/`, `${room}/\uffff`));
          tx.oncomplete = tx.onerror = tx.onabort = () => resolve();
        } catch {
          resolve();
        }
      });
    } catch {
      /* best effort */
    }
  }
  async putMedia(room: string, name: string, envelope: Uint8Array): Promise<void> {
    const rec: MediaRec = { envelope, saved_at: new Date().toISOString() };
    await this.run<unknown>(MEDIA_STORE, 'readwrite', undefined, (s) => s.put(rec, `${room}/${name}`));
  }
  async getMedia(room: string, name: string): Promise<Uint8Array | null> {
    const rec = validMediaRec(await this.run<unknown>(MEDIA_STORE, 'readonly', null, (s) => s.get(`${room}/${name}`)));
    return rec ? rec.envelope : null;
  }
}

/** The page's store: IndexedDB where there is one (a no-op where there is not). */
export function defaultCopyStore(): CopyStore {
  return new IndexedDbCopyStore();
}
