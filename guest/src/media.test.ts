// @vitest-environment node
// GuestMedia (media.ts) against a fake room: a map of sealed blobs by name, the host's answers played by
// the test. Node, for crypto.subtle.
import { setTimeout as sleep } from 'node:timers/promises';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { PreparedImage } from '$lib/notes/ui/types.js';
import { MemoryCopyStore } from './copy';
import { deriveKeys, mediaName, newKey, newRoom, sealMedia, type GuestKeys } from './format';
import { GuestMedia, MISSING_RETRY_MS, sniff, type MediaRoom, type UrlMaker } from './media';
import { MediaError } from './room';

const hex = (b: Uint8Array) => Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
const sha256 = async (b: Uint8Array) => hex(new Uint8Array(await crypto.subtle.digest('SHA-256', b as BufferSource)));

function webp(fill: number, n = 64): Uint8Array {
  const b = new Uint8Array(n).fill(fill);
  b.set([0x52, 0x49, 0x46, 0x46, 0, 0, 0, 0, 0x57, 0x45, 0x42, 0x50]);
  return b;
}
function jpeg(fill: number): Uint8Array {
  const b = new Uint8Array(40).fill(fill);
  b.set([0xff, 0xd8, 0xff]);
  return b;
}
const gif = new Uint8Array([0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 1, 2, 3, 4, 5, 6]);

async function prepared(bytes: Uint8Array, ext: 'webp' | 'jpeg' = 'webp'): Promise<PreparedImage> {
  return { bytes, ext, w: 10, h: 10, sha: await sha256(bytes) };
}

/** Counts the URLs it made and keeps their bytes. */
class Urls implements UrlMaker {
  made = new Map<string, { bytes: Uint8Array; mime: string }>();
  revoked: string[] = [];
  create(bytes: Uint8Array, mime: string): string {
    const u = `blob:test/${this.made.size + 1}`;
    this.made.set(u, { bytes, mime });
    return u;
  }
  revoke(url: string): void {
    this.revoked.push(url);
  }
}

class FakeRoom implements MediaRoom {
  blobs = new Map<string, Uint8Array>();
  gets: string[] = [];
  puts: string[] = [];
  /** Make putMedia reject with this code. */
  refuse: string | null = null;
  down = false;
  async putMedia(name: string, env: Uint8Array): Promise<void> {
    this.puts.push(name);
    if (this.refuse) throw new MediaError(this.refuse);
    if (!this.blobs.has(name)) this.blobs.set(name, env.slice());
  }
  async getMedia(name: string): Promise<Uint8Array> {
    this.gets.push(name);
    if (this.down) throw new MediaError('closed');
    const b = this.blobs.get(name);
    if (!b) throw new MediaError('media_missing');
    return b.slice();
  }
}

let keys: GuestKeys;
let room: string;
beforeEach(async () => {
  keys = await deriveKeys(newKey());
  room = newRoom();
});
afterEach(() => {
  vi.useRealTimers();
});

function make(over: { copies?: MemoryCopyStore; transport?: FakeRoom; keep?: () => boolean } = {}) {
  const transport = over.transport ?? new FakeRoom();
  const copies = over.copies ?? new MemoryCopyStore();
  const urls = new Urls();
  const media = new GuestMedia({ room, keys, transport, copies, urls, keep: over.keep });
  const heard: string[] = [];
  media.subscribe((sha) => heard.push(sha));
  return { media, transport, copies, urls, heard };
}

/** Let the real async work (WebCrypto runs off the timers) finish. */
async function settle(): Promise<void> {
  await sleep(5);
}
/** Move the fake clock, then let what it started finish. */
async function adv(ms: number): Promise<void> {
  vi.advanceTimersByTime(ms);
  await settle();
}

/** Until the provider has an URL for `sha` (it fetches in the background). */
async function until(cond: () => boolean): Promise<void> {
  for (let i = 0; i < 200 && !cond(); i++) await Promise.resolve().then(() => new Promise((r) => setTimeout(r, 0)));
  if (!cond()) throw new Error('did not happen');
}

describe('sniff', () => {
  it('knows WebP and JPEG by their magic and nothing else', () => {
    expect(sniff(webp(1))).toBe('webp');
    expect(sniff(jpeg(1))).toBe('jpeg');
    expect(sniff(gif)).toBeNull();
    expect(sniff(new TextEncoder().encode('<svg xmlns="http://www.w3.org/2000/svg"/>'))).toBeNull();
    expect(sniff(new Uint8Array([0x52, 0x49, 0x46, 0x46, 0, 0, 0, 0, 0x57, 0x41, 0x56, 0x45]))).toBeNull(); // RIFF WAVE
    expect(sniff(new Uint8Array())).toBeNull();
  });
});

describe('GuestMedia upload', () => {
  it('seals the bytes, stores them under the HMAC name, shows them at once and keeps a copy', async () => {
    const { media, transport, copies, urls, heard } = make();
    const img = await prepared(webp(7));
    const done = media.upload(img);
    expect(media.state(img.sha)).toBe('uploading');
    const url = media.url(img.sha);
    expect(url).toBe('blob:test/1');
    expect(urls.made.get(url!)!.mime).toBe('image/webp');
    await done;
    expect(media.state(img.sha)).toBeNull();
    const name = await mediaName(keys, img.sha);
    expect(transport.puts).toEqual([name]);
    // what the room holds opens with the key and is the image
    const env = transport.blobs.get(name)!;
    expect(env[0]).toBe(6);
    const again = await sealMedia(keys, room, img.sha, img.bytes);
    expect(env.length).toBe(again.length);
    // the saved copy has the same envelope
    expect(Array.from((await copies.getMedia(room, name))!)).toEqual(Array.from(env));
    expect(heard).toContain(img.sha);
    expect(transport.gets).toEqual([]); // our own bytes were never fetched
  });

  it('is failed when the room refuses; upload again retries and succeeds', async () => {
    const { media, transport } = make();
    const img = await prepared(jpeg(3));
    transport.refuse = 'room_media_full';
    await expect(media.upload(img)).rejects.toMatchObject({ code: 'room_media_full' });
    expect(media.state(img.sha)).toBe('failed');
    expect(media.url(img.sha)).not.toBeNull(); // still shown from our bytes
    transport.refuse = null;
    await media.upload(img);
    expect(media.state(img.sha)).toBeNull();
    expect(transport.blobs.size).toBe(1);
  });

  it('shares one upload between two calls for the same image', async () => {
    const { media, transport } = make();
    const img = await prepared(webp(5));
    await Promise.all([media.upload(img), media.upload(img)]);
    expect(transport.puts).toHaveLength(1);
  });

  it('does not keep the copy when it cannot exist', async () => {
    const { media, copies } = make({ keep: () => false });
    const img = await prepared(webp(5));
    await media.upload(img);
    expect(copies.media.size).toBe(0);
  });
});

describe('GuestMedia url', () => {
  async function seeded(transport: FakeRoom, bytes: Uint8Array) {
    const img = await prepared(bytes);
    const name = await mediaName(keys, img.sha);
    transport.blobs.set(name, await sealMedia(keys, room, img.sha, bytes));
    return { img, name };
  }

  it('fetches from the room, opens it, shows it and keeps it for a reload', async () => {
    const { media, transport, copies, urls, heard } = make();
    const { img, name } = await seeded(transport, webp(9));
    expect(media.url(img.sha)).toBeNull();
    expect(media.url(img.sha)).toBeNull(); // one ask only
    await until(() => media.url(img.sha) !== null);
    expect(transport.gets).toEqual([name]);
    const u = media.url(img.sha)!;
    expect(Array.from(urls.made.get(u)!.bytes)).toEqual(Array.from(img.bytes));
    expect(heard).toEqual([img.sha]);
    expect(copies.media.has(`${room}/${name}`)).toBe(true);

    // a reload: new provider, same browser copy, the room unreachable
    const down = new FakeRoom();
    down.down = true;
    const second = make({ copies, transport: down });
    second.media.url(img.sha);
    await until(() => second.media.url(img.sha) !== null);
    expect(down.gets).toEqual([]);
    expect(second.urls.made.get(second.media.url(img.sha)!)!.mime).toBe('image/webp');
  });

  it('a JPEG gets image/jpeg', async () => {
    const { media, transport, urls } = make();
    const { img } = await seeded(transport, jpeg(2));
    media.url(img.sha);
    await until(() => media.url(img.sha) !== null);
    expect(urls.made.get(media.url(img.sha)!)!.mime).toBe('image/jpeg');
  });

  it('never makes a URL for a sealed GIF, SVG or other non-image', async () => {
    const { media, transport, urls } = make();
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    const { img } = await seeded(transport, gif);
    media.url(img.sha);
    await adv(0);
    expect(media.url(img.sha)).toBeNull();
    expect(urls.made.size).toBe(0);
    media.dispose();
  });

  it('treats a blob sealed for another image as missing', async () => {
    const { media, transport, urls } = make();
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    const a = await prepared(webp(1));
    const b = await prepared(webp(2));
    // someone put b's envelope under a's name
    transport.blobs.set(await mediaName(keys, a.sha), await sealMedia(keys, room, b.sha, b.bytes));
    media.url(a.sha);
    await adv(0);
    expect(media.url(a.sha)).toBeNull();
    expect(urls.made.size).toBe(0);
    media.dispose();
  });

  it('an altered copy is ignored and the room is asked', async () => {
    const copies = new MemoryCopyStore();
    const first = make({ copies });
    const { img, name } = await seeded(first.transport, webp(4));
    await copies.putMedia(room, name, new Uint8Array([6, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28]));
    first.media.url(img.sha);
    await until(() => first.media.url(img.sha) !== null);
    expect(first.transport.gets).toEqual([name]);
  });

  it('ignores something that is not a sha', () => {
    const { media, transport } = make();
    expect(media.url('nope')).toBeNull();
    expect(transport.gets).toEqual([]);
  });
});

describe('GuestMedia missing blobs', () => {
  it('asks again after 10, 30 and 60 s, then is missing; a broadcast brings it back at once', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    const { media, transport, heard } = make();
    const img = await prepared(webp(8));
    const name = await mediaName(keys, img.sha);
    media.url(img.sha);
    await adv(0);
    expect(transport.gets).toHaveLength(1);
    expect(media.state(img.sha)).toBeNull(); // still loading, not missing
    await adv(MISSING_RETRY_MS[0] - 1);
    expect(transport.gets).toHaveLength(1);
    await adv(1);
    expect(transport.gets).toHaveLength(2);
    await adv(MISSING_RETRY_MS[1]);
    expect(transport.gets).toHaveLength(3);
    expect(media.state(img.sha)).toBeNull();
    await adv(MISSING_RETRY_MS[2]);
    expect(transport.gets).toHaveLength(4);
    expect(media.state(img.sha)).toBe('missing');
    expect(heard).toEqual([img.sha]); // told once, when it gave up
    // nothing more on its own, however often it is asked for
    media.url(img.sha);
    await adv(5 * 60_000);
    expect(transport.gets).toHaveLength(4);

    // the blob lands and the host says so
    transport.blobs.set(name, await sealMedia(keys, room, img.sha, img.bytes));
    media.heard(name);
    await adv(0);
    expect(media.state(img.sha)).toBeNull();
    expect(media.url(img.sha)).not.toBeNull();
    media.dispose();
  });

  it('a broadcast during a wait asks at once and shows the image', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    const { media, transport } = make();
    const img = await prepared(webp(6));
    const name = await mediaName(keys, img.sha);
    media.url(img.sha);
    await adv(0);
    expect(transport.gets).toHaveLength(1);
    transport.blobs.set(name, await sealMedia(keys, room, img.sha, img.bytes));
    media.heard(name);
    await adv(0);
    expect(transport.gets).toHaveLength(2);
    expect(media.url(img.sha)).not.toBeNull();
    // the stale timer does not ask a third time
    await adv(MISSING_RETRY_MS[0] * 2);
    expect(transport.gets).toHaveLength(2);
    media.dispose();
  });

  it('a room that cannot be reached counts like a missing blob', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    const { media, transport } = make();
    transport.down = true;
    const sha = 'ab'.repeat(32);
    media.url(sha);
    await adv(0);
    for (const ms of MISSING_RETRY_MS) await adv(ms);
    expect(transport.gets).toHaveLength(MISSING_RETRY_MS.length + 1);
    expect(media.state(sha)).toBe('missing');
    media.dispose();
  });

  it('dispose revokes the URLs and stops the timers', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    const { media, transport, urls } = make();
    const img = await prepared(webp(3));
    await media.upload(img);
    media.url('cd'.repeat(32));
    await adv(0);
    media.dispose();
    expect(urls.revoked).toEqual(['blob:test/1']);
    await adv(5 * 60_000);
    expect(transport.gets).toHaveLength(1);
    expect(media.url(img.sha)).toBeNull();
  });
});
