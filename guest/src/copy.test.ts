// The saved copy's stores. IndexedDB itself is not exercised here: fake-indexeddb is not a
// dependency of this repo, so only the shape checks, the expiry rule, the in-memory store and the
// "no indexedDB means a no-op" promise of the real one are.
import { describe, expect, it } from 'vitest';
import { IndexedDbCopyStore, MemoryCopyStore, expired, validRec, type CopyRec } from './copy';

const rec = (over: Partial<CopyRec> = {}): CopyRec => ({
  envelope: new Uint8Array([3, 1, 2, 3]),
  expires: '2026-11-01T00:00:00.000Z',
  ended_at: null,
  saved_at: '2026-10-25T10:00:00.000Z',
  ...over
});

describe('MemoryCopyStore', () => {
  it('saves, loads, replaces and removes by room', async () => {
    const s = new MemoryCopyStore();
    expect(await s.load('a')).toBeNull();
    await s.save('a', rec());
    await s.save('b', rec({ ended_at: '2026-10-25T11:00:00.000Z' }));
    expect(await s.load('a')).toEqual(rec());
    expect((await s.load('b'))?.ended_at).toBe('2026-10-25T11:00:00.000Z');
    await s.save('a', rec({ envelope: new Uint8Array([9]) }));
    expect(Array.from((await s.load('a'))!.envelope)).toEqual([9]);
    await s.remove('a');
    expect(await s.load('a')).toBeNull();
    expect(await s.load('b')).not.toBeNull();
    await s.remove('nope'); // nothing to remove is fine
  });

  it('keeps its own bytes', async () => {
    const s = new MemoryCopyStore();
    const r = rec();
    await s.save('a', r);
    r.envelope[0] = 0;
    expect((await s.load('a'))!.envelope[0]).toBe(3);
  });
});

describe('validRec', () => {
  it('takes a record and drops anything extra, the key included', () => {
    const v = validRec({ ...rec(), key: 'secret', meta_title: 'Weekly' });
    expect(v).toEqual(rec());
    expect(Object.keys(v!).sort()).toEqual(['ended_at', 'envelope', 'expires', 'saved_at']);
  });
  it('refuses anything else', () => {
    expect(validRec(null)).toBeNull();
    expect(validRec('x')).toBeNull();
    expect(validRec({ ...rec(), envelope: [1, 2] })).toBeNull();
    expect(validRec({ ...rec(), envelope: new Uint8Array() })).toBeNull();
    expect(validRec({ ...rec(), expires: 5 })).toBeNull();
    expect(validRec({ ...rec(), ended_at: 5 })).toBeNull();
    expect(validRec({ ...rec(), saved_at: undefined })).toBeNull();
  });
});

describe('expired', () => {
  it('is true from the expiry on, and for a time nobody can read', () => {
    const at = Date.parse('2026-11-01T00:00:00.000Z');
    expect(expired(rec(), at - 1)).toBe(false);
    expect(expired(rec(), at)).toBe(true);
    expect(expired(rec({ expires: 'soon' }), 0)).toBe(true);
  });
});

describe('IndexedDbCopyStore without IndexedDB', () => {
  it('is a no-op that never throws', async () => {
    expect(typeof indexedDB).toBe('undefined');
    const s = new IndexedDbCopyStore();
    await s.save('a', rec());
    expect(await s.load('a')).toBeNull();
    await s.remove('a');
  });
});
