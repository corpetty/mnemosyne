// The guest page's saved copy (phase 10, C15): the last sealed snapshot of a room, kept in this
// browser until the link expires, so a guest who already opened the notes can reread them while
// the owner's computer is off. Only what the host sent is stored, exactly as received: the
// envelope (opened again with the key from the link each time) and the times around it. Never the
// key, never plaintext, so not the title or the owner either (they are inside the envelope).
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

export interface CopyStore {
  save(room: string, rec: CopyRec): Promise<void>;
  load(room: string): Promise<CopyRec | null>;
  remove(room: string): Promise<void>;
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

/** Has the copy's link expired at `nowMs`? An unreadable time counts as expired. */
export function expired(rec: CopyRec, nowMs: number): boolean {
  const t = Date.parse(rec.expires);
  return !Number.isFinite(t) || t <= nowMs;
}

/** For tests (and anywhere without storage). */
export class MemoryCopyStore implements CopyStore {
  readonly copies = new Map<string, CopyRec>();
  async save(room: string, rec: CopyRec): Promise<void> {
    this.copies.set(room, { ...rec, envelope: rec.envelope.slice() });
  }
  async load(room: string): Promise<CopyRec | null> {
    const r = this.copies.get(room);
    return r ? { ...r, envelope: r.envelope.slice() } : null;
  }
  async remove(room: string): Promise<void> {
    this.copies.delete(room);
  }
}

export const DB_NAME = 'mnemosyne-guest';
export const STORE_NAME = 'copies';

/** IndexedDB `mnemosyne-guest` / `copies`, one record per room id. */
export class IndexedDbCopyStore implements CopyStore {
  private opening: Promise<IDBDatabase | null> | null = null;

  private db(): Promise<IDBDatabase | null> {
    this.opening ??= new Promise<IDBDatabase | null>((resolve) => {
      try {
        const req = indexedDB.open(DB_NAME, 1);
        req.onupgradeneeded = () => {
          try {
            req.result.createObjectStore(STORE_NAME);
          } catch {
            /* the error below */
          }
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => resolve(null);
        req.onblocked = () => resolve(null);
      } catch {
        resolve(null); // no indexedDB, or it throws (some private windows)
      }
    });
    return this.opening;
  }

  /** Run one request in a transaction; resolves with its result, or `fallback` on any failure. */
  private async run<T>(mode: IDBTransactionMode, fallback: T, make: (s: IDBObjectStore) => IDBRequest): Promise<T> {
    try {
      const db = await this.db();
      if (!db) return fallback;
      return await new Promise<T>((resolve) => {
        try {
          const tx = db.transaction(STORE_NAME, mode);
          const req = make(tx.objectStore(STORE_NAME));
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
    await this.run<unknown>('readwrite', undefined, (s) => s.put(rec, room));
  }
  async load(room: string): Promise<CopyRec | null> {
    return validRec(await this.run<unknown>('readonly', null, (s) => s.get(room)));
  }
  async remove(room: string): Promise<void> {
    await this.run<unknown>('readwrite', undefined, (s) => s.delete(room));
  }
}

/** The page's store: IndexedDB where there is one (a no-op where there is not). */
export function defaultCopyStore(): CopyStore {
  return new IndexedDbCopyStore();
}
