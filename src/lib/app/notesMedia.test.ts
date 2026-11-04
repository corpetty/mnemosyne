import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { PreparedImage } from '$lib/notes/ui/types.js';
import { createNotesMedia, type NotesMediaApi } from './notesMedia.js';

const SHA = 'a'.repeat(64);
const blob = new Blob(['x'], { type: 'image/webp' });
const flush = () => vi.advanceTimersByTimeAsync(0);

let n = 0;
let revoked: string[] = [];
beforeEach(() => {
  vi.useFakeTimers();
  n = 0;
  revoked = [];
  URL.createObjectURL = vi.fn(() => `blob:${++n}`);
  URL.revokeObjectURL = vi.fn((u: string) => void revoked.push(u));
});
afterEach(() => vi.useRealTimers());

function setup(get: NotesMediaApi['get'], put: NotesMediaApi['put'] = vi.fn(async () => {})) {
  const api = { get: vi.fn(get), put: vi.fn(put) };
  const m = createNotesMedia('s1', api);
  const seen: string[] = [];
  m.subscribe((s) => seen.push(s));
  return { api, m, seen };
}

const image: PreparedImage = { bytes: new Uint8Array([1, 2]), ext: 'webp', w: 1, h: 1, sha: SHA };

describe('notes media provider', () => {
  it('fetches once, returns null now and notifies when it lands', async () => {
    const { api, m, seen } = setup(async () => blob);
    expect(m.url(SHA)).toBeNull();
    expect(m.url(SHA)).toBeNull();
    await flush();
    expect(api.get).toHaveBeenCalledTimes(1);
    expect(seen).toEqual([SHA]);
    expect(m.url(SHA)).toBe('blob:1');
    expect(api.get).toHaveBeenCalledTimes(1);
  });

  it('a 404 is retried on the event for that sha', async () => {
    let have = false;
    const { api, m, seen } = setup(async () => (have ? blob : null));
    m.url(SHA);
    await flush();
    expect(m.url(SHA)).toBeNull();
    expect(m.state!(SHA)).toBeNull();
    have = true;
    m.event(SHA);
    await flush();
    expect(api.get).toHaveBeenCalledTimes(2);
    expect(seen).toEqual([SHA]);
    expect(m.url(SHA)).toBe('blob:1');
  });

  it('retries at 10, 30 and 60 s, then reports missing until a new event', async () => {
    let have = false;
    const { api, m, seen } = setup(async () => (have ? blob : null));
    m.url(SHA);
    await flush();
    await vi.advanceTimersByTimeAsync(9_999);
    expect(api.get).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(api.get).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(api.get).toHaveBeenCalledTimes(3);
    expect(m.state!(SHA)).toBeNull();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(api.get).toHaveBeenCalledTimes(4);
    expect(m.state!(SHA)).toBe('missing');
    expect(seen).toEqual([SHA]);
    expect(m.url(SHA)).toBeNull(); // no new fetch while missing
    await vi.advanceTimersByTimeAsync(300_000);
    expect(api.get).toHaveBeenCalledTimes(4);
    have = true;
    m.event(SHA);
    await flush();
    expect(m.state!(SHA)).toBeNull();
    expect(m.url(SHA)).toBe('blob:1');
  });

  it('an upload is uploading, then its bytes are the picture without a fetch', async () => {
    let done!: () => void;
    const { api, m, seen } = setup(async () => null, () => new Promise<void>((r) => (done = r)));
    const p = m.upload(image);
    expect(m.state!(SHA)).toBe('uploading');
    done();
    await p;
    expect(api.put).toHaveBeenCalledWith('s1', SHA, image.bytes, 'image/webp');
    expect(m.state!(SHA)).toBeNull();
    expect(m.url(SHA)).toBe('blob:1');
    expect(api.get).not.toHaveBeenCalled();
    expect(seen).toEqual([SHA, SHA]);
  });

  it('a failed upload is failed and rejects; a retry can succeed', async () => {
    const put = vi.fn().mockRejectedValueOnce(new Error('413: too big')).mockResolvedValue(undefined);
    const { m } = setup(async () => null, put);
    await expect(m.upload(image)).rejects.toThrow('413');
    expect(m.state!(SHA)).toBe('failed');
    await m.upload({ ...image, ext: 'jpeg' });
    expect(put).toHaveBeenLastCalledWith('s1', SHA, image.bytes, 'image/jpeg');
    expect(m.state!(SHA)).toBeNull();
  });

  it('dispose revokes the object URLs and stops the timers', async () => {
    const { api, m } = setup(async () => blob);
    m.url(SHA);
    await flush();
    const other = 'b'.repeat(64);
    const missing = setup(async () => null);
    missing.m.url(other);
    await flush();
    m.dispose();
    missing.m.dispose();
    expect(revoked).toEqual(['blob:1']);
    await vi.advanceTimersByTimeAsync(100_000);
    expect(missing.api.get).toHaveBeenCalledTimes(1);
    expect(api.get).toHaveBeenCalledTimes(1);
  });
});
