// @vitest-environment node
// GuestDoc against an in-memory publish host speaking /live/<room> (publish-host/src/live.rs):
// a log with seqs, an optional sealed snapshot, frames relayed to the others, acks to the sender.
// Node, for crypto.subtle (jsdom has none); the wasm is loaded by the notes test harness.
import { afterEach, describe, expect, it } from 'vitest';
import { openDoc } from '$lib/notes/model.js';
import { ORIGIN, WasmHashWeb, sameBytes } from '$lib/notes/test-harness.js';
import { insertText, setLink } from '$lib/notes/commands.js';
import { ARTIFACT_TAG, DELTA_TAG, drain } from '$lib/notes/sync.js';
import { MemoryCopyStore, type CopyRec } from './copy';
import { DEBOUNCE_MS, GuestDoc, parseMeta, PRESENCE_TTL_MS, SEL_EVERY_MS, cleanSel, type GuestDocOptions } from './doc';
import { createGuestPresence } from './presence';
import {
  Kind,
  b64urlEncode,
  deriveKeys,
  newKey,
  newRoom,
  sealJson,
  sealNotes,
  unseal,
  unsealNotes,
  type GuestKeys
} from './format';
import type { RoomSocket } from './room';

const LONG_URL = 'https://example.com/long/enough/to/be/an/artifact';

class HostSocket implements RoomSocket {
  readyState = 0;
  binaryType = '';
  onopen: RoomSocket['onopen'] = null;
  onmessage: RoomSocket['onmessage'] = null;
  onclose: RoomSocket['onclose'] = null;
  onerror: RoomSocket['onerror'] = null;
  joined = false;
  since = -1;

  constructor(
    public url: string,
    private host: Host
  ) {
    host.sockets.push(this);
    queueMicrotask(() => {
      if (this.readyState !== 0) return;
      this.readyState = 1;
      this.onopen?.({});
    });
  }
  send(data: string | Uint8Array): void {
    if (this.readyState !== 1) throw new Error('socket not open');
    const copy = typeof data === 'string' ? data : data.slice();
    queueMicrotask(() => this.host.fromClient(this, copy));
  }
  close(code = 1000): void {
    if (this.readyState >= 2) return;
    this.readyState = 3;
    queueMicrotask(() => this.onclose?.({ code }));
  }
  // host side
  deliver(data: string | Uint8Array): void {
    const d = typeof data === 'string' ? data : data.slice().buffer;
    queueMicrotask(() => {
      if (this.readyState === 1) this.onmessage?.({ data: d });
    });
  }
  json(msg: object): void {
    this.deliver(JSON.stringify(msg));
  }
  drop(code = 1006): void {
    if (this.readyState >= 2) return;
    this.readyState = 3;
    this.joined = false;
    queueMicrotask(() => this.onclose?.({ code }));
  }
}

function tagged(tag: number, seq: number | null, env: Uint8Array): Uint8Array {
  const head = seq === null ? 1 : 9;
  const out = new Uint8Array(head + env.length);
  out[0] = tag;
  if (seq !== null) new DataView(out.buffer).setBigUint64(1, BigInt(seq), false);
  out.set(env, head);
  return out;
}

class Host {
  sockets: HostSocket[] = [];
  log: { seq: number; env: Uint8Array; from: HostSocket | null }[] = [];
  seq = 0;
  snapshot: { seq: number; env: Uint8Array } | null = null;
  endedAt: number | null = null;
  /** Frames from clients vanish (the network lost them). */
  deaf = false;
  /** Acks are not sent (the connection dies before they arrive). */
  noAcks = false;
  /** Every presence envelope a client sent, in order. */
  presences: Uint8Array[] = [];
  Socket: new (url: string) => HostSocket;

  constructor(
    public room: string,
    public keys: GuestKeys
  ) {
    const host = this;
    this.Socket = class extends HostSocket {
      constructor(url: string) {
        super(url, host);
      }
    };
  }

  static async create(): Promise<Host> {
    return new Host(newRoom(), await deriveKeys(newKey()));
  }

  get live(): HostSocket[] {
    return this.sockets.filter((s) => s.joined && s.readyState === 1);
  }

  fromClient(s: HostSocket, data: string | Uint8Array): void {
    if (s.readyState !== 1) return;
    if (typeof data === 'string') {
      if (data === 'ping') return s.deliver('pong');
      const msg = JSON.parse(data);
      if (msg.type !== 'join' || msg.token !== this.keys.roomToken) return s.drop(4401);
      this.join(s, msg.since);
      return;
    }
    if (!s.joined) return s.drop(4401);
    const env = data.slice(1);
    if (data[0] === 0x01) {
      if (this.endedAt !== null) return s.drop(4403);
      if (this.deaf) return;
      this.append(env, s);
    } else if (data[0] === 0x02) {
      this.presences.push(env);
      for (const o of this.live) if (o !== s) o.deliver(tagged(0x02, null, env));
    }
  }

  private join(s: HostSocket, since: number): void {
    s.joined = true;
    s.since = since;
    const snap = this.snapshot && (since === 0 || since < this.snapshot.seq) ? this.snapshot : null;
    s.json({
      type: 'welcome',
      seq: this.seq,
      snapshot_seq: this.snapshot ? this.snapshot.seq : null,
      ended: this.endedAt !== null,
      ended_at: this.endedAt,
      expires: 9999,
      present: this.live.length
    });
    if (snap) s.deliver(tagged(0x03, snap.seq, snap.env));
    const floor = Math.max(since, snap ? snap.seq : 0);
    for (const f of this.log) if (f.seq > floor) s.deliver(tagged(0x01, f.seq, f.env));
    for (const o of this.live) o.json({ type: 'count', present: this.live.length });
  }

  /** Log and relay; ack the sender. `from` null: the owner's bridge. */
  append(env: Uint8Array, from: HostSocket | null): number {
    const seq = ++this.seq;
    this.log.push({ seq, env, from });
    for (const o of this.live) {
      if (o === from) {
        if (!this.noAcks) o.json({ type: 'ack', seq });
      } else o.deliver(tagged(0x01, seq, env));
    }
    return seq;
  }

  /** The owner posts a snapshot covering the log so far, which is pruned. */
  async postSnapshot(obj: Record<string, unknown>): Promise<void> {
    const env = await sealJson(this.keys, this.room, Kind.SNAPSHOT, { format: 1, threads: [], meta: {}, ...obj });
    this.snapshot = { seq: this.seq, env };
    this.log = this.log.filter((f) => f.seq > this.seq);
  }

  end(): void {
    this.endedAt = 1234;
    for (const o of this.live) o.json({ type: 'ended', ended_at: this.endedAt, expires: 5678 });
  }

  /** The presence objects a guest sent (by gid), in order. */
  async presencesFrom(gid: string): Promise<Record<string, unknown>[]> {
    const out: Record<string, unknown>[] = [];
    for (const env of this.presences) {
      const { plaintext } = await unseal(this.keys, this.room, env);
      const o = JSON.parse(new TextDecoder().decode(plaintext));
      if (o.gid === gid) out.push(o);
    }
    return out;
  }

  /** Deliver a presence from the owner's side (the bridge) to everyone live. */
  async fromOwner(obj: Record<string, unknown>): Promise<void> {
    const env = await sealJson(this.keys, this.room, Kind.PRESENCE, obj);
    for (const s of this.live) s.deliver(tagged(0x02, null, env));
  }

  /** Raw notes frames a guest sent, in order. */
  async framesFrom(gid: string): Promise<Uint8Array[]> {
    const out: Uint8Array[] = [];
    for (const f of this.log) {
      if (f.env[0] !== Kind.NOTES) continue;
      const n = await unsealNotes(this.keys, this.room, f.env);
      if (n.gid === gid) out.push(n.frame);
    }
    return out;
  }
}

const docs: GuestDoc[] = [];

function guest(host: Host, name: string, opts: Partial<GuestDocOptions> = {}): GuestDoc {
  const d = new GuestDoc({
    room: host.room,
    keys: host.keys,
    gid: `g-${name}`,
    name,
    url: `ws://host/live/${host.room}?who=${name}`,
    webSocket: host.Socket,
    origin: ORIGIN,
    ...opts
  });
  docs.push(d);
  d.connect();
  return d;
}

afterEach(() => {
  for (const d of docs.splice(0)) d.destroy();
});

const tick = (ms = 2) => new Promise((r) => setTimeout(r, ms));

function text(d: GuestDoc): string | null {
  return d.doc ? d.doc.web.text(d.doc.obj) : null;
}

function append(d: GuestDoc, s: string): void {
  d.applyLocal((doc) => insertText(doc, doc.web.textLen(doc.obj), s));
}

/** Until nothing moves: every doc idle, the log and every text unchanged for a few rounds. */
async function settle(host: Host, ...ds: GuestDoc[]): Promise<void> {
  let last = '';
  let same = 0;
  for (let i = 0; i < 400; i++) {
    await tick();
    await Promise.all(ds.map((d) => d.idle()));
    const sig = JSON.stringify([host.seq, host.sockets.length, ds.map((d) => [d.status, text(d), d.version])]);
    if (sig === last) {
      if (++same >= 3) return;
    } else same = 0;
    last = sig;
  }
  throw new Error('did not settle');
}

function converged(...ds: GuestDoc[]): boolean {
  const a = ds[0].doc!;
  return ds.every((d) => d.doc !== null && sameBytes(d.doc.web.encode(), a.web.encode()) && text(d) === text(ds[0]));
}

/** The doc's joined socket (each test doc has its own URL). */
function socketOf(host: Host, d: GuestDoc): HostSocket {
  const s = host.live.filter((x) => x.url.endsWith(`?who=${d.you.name}`));
  return s[s.length - 1];
}

describe('GuestDoc', () => {
  it('two guests converge on concurrent edits through the room', async () => {
    const host = await Host.create();
    const A = guest(host, 'Ana');
    const B = guest(host, 'Ben');
    await settle(host, A, B);
    expect(A.status).toBe('live');
    expect(B.status).toBe('live');
    expect(text(A)).toBe('');
    // at the same time, at the same place
    // (different texts: hashseq ids are content hashes, so identical concurrent inserts coincide)
    append(A, 'ana says hi\n');
    append(B, 'ben writes\n');
    A.flush();
    B.flush();
    await settle(host, A, B);
    expect(converged(A, B)).toBe(true);
    expect(text(A)).toContain('ana says hi\n');
    expect(text(A)).toContain('ben writes\n');
    // the debounce sends without a flush
    append(B, 'later\n');
    await tick(DEBOUNCE_MS + 20);
    await settle(host, A, B);
    expect(text(A)).toContain('later\n');
    expect(converged(A, B)).toBe(true);
    // every frame carries its guest's id and name
    expect((await host.framesFrom('g-Ana')).length).toBeGreaterThan(0);
    expect((await host.framesFrom('g-Ben')).length).toBeGreaterThan(0);
  });

  it('a late joiner opens from the snapshot and the frames after it', async () => {
    const host = await Host.create();
    const A = guest(host, 'Ana');
    const B = guest(host, 'Ben');
    await settle(host, A, B);
    append(A, 'one\n');
    A.flush();
    await settle(host, A, B);
    append(B, 'two\n');
    B.flush();
    await settle(host, A, B);
    // what the owner's bridge posts: the folded store, as the app's relay hello would send it
    await host.postSnapshot({
      notes: b64urlEncode(A.doc!.web.encode()),
      frames: [],
      origin: ORIGIN,
      meta: { title: 'Weekly', date: '2026-10-24T10:00:00+02:00', owner: 'Olga', agenda: [{ text: 'Budget', covered: true }], ended: false, ended_at: '' },
      threads: [{ root: { id: 'c1', parent_id: null, author: 'Olga', gid: '', text: 'hi', quote: 'one', mark: 'comment:c1' }, replies: [] }]
    });
    expect(host.log.length).toBe(0);
    append(A, 'three\n');
    A.flush();
    await settle(host, A, B);
    expect(host.log.length).toBeGreaterThan(0);
    // no origin given: it comes with the snapshot
    const C = guest(host, 'Cy', { origin: undefined });
    await settle(host, A, B, C);
    expect(C.status).toBe('live');
    expect(converged(A, B, C)).toBe(true);
    expect(C.meta?.title).toBe('Weekly');
    expect(C.meta?.agenda).toEqual([{ text: 'Budget', covered: true }]);
    expect(C.threads.map((t) => t.root.id)).toEqual(['c1']);
    // and C's edits reach the others
    append(C, 'four\n');
    C.flush();
    await settle(host, A, B, C);
    expect(converged(A, B, C)).toBe(true);
    expect(text(A)).toContain('four\n');
  });

  it('opens from a snapshot made of frames only, with the origin from META', async () => {
    const host = await Host.create();
    const A = guest(host, 'Ana');
    await settle(host, A);
    append(A, 'alpha\n');
    A.applyLocal((doc) => setLink(doc, 0, 5, LONG_URL));
    A.flush();
    await settle(host, A);
    const frames = await host.framesFrom('g-Ana');
    expect(frames.some((f) => f[0] === ARTIFACT_TAG)).toBe(true);
    expect(frames.some((f) => f[0] === DELTA_TAG)).toBe(true);
    await host.postSnapshot({ notes: '', frames: frames.map(b64urlEncode) });
    const B = guest(host, 'Ben', { origin: undefined });
    await settle(host, A, B);
    expect(B.doc).toBe(null); // the origin is not known yet
    expect(B.status).toBe('live');
    const meta = { title: 'T', date: '', owner: 'Olga', agenda: [], ended: false, ended_at: '', origin: ORIGIN };
    host.append(await sealJson(host.keys, host.room, Kind.META, meta), null);
    await settle(host, A, B);
    expect(B.doc).not.toBe(null);
    expect(converged(A, B)).toBe(true);
    expect(B.meta?.owner).toBe('Olga');
  });

  it('edits made while disconnected are sent exactly once after the reconnect', async () => {
    const host = await Host.create();
    const A = guest(host, 'Ana');
    const B = guest(host, 'Ben');
    await settle(host, A, B);
    append(A, 'before\n');
    A.flush();
    await settle(host, A, B);
    const sentBefore = A.sentFrames;
    const loggedBefore = (await host.framesFrom('g-Ana')).length;

    socketOf(host, A).drop(1006);
    await tick(5);
    expect(A.status).toBe('reconnecting');
    // typing goes on locally; nothing is sent or queued
    append(A, 'offline one\n');
    A.flush();
    append(A, 'offline two\n');
    A.applyLocal((doc) => setLink(doc, 0, 6, LONG_URL));
    A.flush();
    expect(A.sentFrames).toBe(sentBefore);
    // meanwhile B types too
    append(B, 'ben while ana was away\n');
    B.flush();
    await settle(host, B);

    A.reconnect();
    await settle(host, A, B);
    expect(A.status).toBe('live');
    expect(converged(A, B)).toBe(true);
    expect(text(B)).toContain('offline one\n');
    expect(text(B)).toContain('offline two\n');
    expect(text(A)).toContain('ben while ana was away\n');
    // one artifact (the link) and one delta with both offline edits; never B's ops sent back
    const after = (await host.framesFrom('g-Ana')).slice(loggedBefore);
    expect(after.map((f) => f[0])).toEqual([ARTIFACT_TAG, DELTA_TAG]);
    expect(A.sentFrames - sentBefore).toBe(2);
    // and nothing more later
    await settle(host, A, B);
    expect((await host.framesFrom('g-Ana')).length).toBe(loggedBefore + 2);
  });

  it('a frame the room logged but never acknowledged is not sent again', async () => {
    const host = await Host.create();
    const A = guest(host, 'Ana');
    const B = guest(host, 'Ben');
    await settle(host, A, B);
    host.noAcks = true;
    append(A, 'logged, ack lost\n');
    A.applyLocal((doc) => setLink(doc, 0, 6, LONG_URL));
    A.flush();
    await settle(host, A, B);
    const logged = (await host.framesFrom('g-Ana')).length;
    expect(logged).toBe(2);
    expect(A.room.since).toBeLessThan(host.seq);
    host.noAcks = false;
    socketOf(host, A).drop(1006);
    await tick(5);
    A.reconnect();
    await settle(host, A, B);
    expect(A.status).toBe('live');
    expect(host.sockets[host.sockets.length - 1].since).toBeLessThan(host.seq);
    expect((await host.framesFrom('g-Ana')).length).toBe(logged);
    expect(A.room.since).toBe(host.seq);
    expect(converged(A, B)).toBe(true);
  });

  it('a frame lost on the way is sent again after the reconnect, once', async () => {
    const host = await Host.create();
    const A = guest(host, 'Ana');
    const B = guest(host, 'Ben');
    await settle(host, A, B);
    host.deaf = true;
    append(A, 'lost on the way\n');
    A.flush();
    await settle(host, A, B);
    expect(text(B)).toBe('');
    host.deaf = false;
    socketOf(host, A).drop(1006);
    await tick(5);
    A.reconnect();
    await settle(host, A, B);
    expect(text(B)).toBe('lost on the way\n');
    expect((await host.framesFrom('g-Ana')).length).toBe(1);
    expect(converged(A, B)).toBe(true);
  });

  it('an artifact the room already has is not sent again', async () => {
    const host = await Host.create();
    const A = guest(host, 'Ana');
    await settle(host, A);
    append(A, 'link\n');
    A.applyLocal((doc) => setLink(doc, 0, 4, LONG_URL));
    A.flush();
    await settle(host, A);
    const logged = (await host.framesFrom('g-Ana')).length;
    socketOf(host, A).drop(1006);
    await tick(5);
    A.reconnect();
    await settle(host, A);
    expect(A.status).toBe('live');
    expect((await host.framesFrom('g-Ana')).length).toBe(logged);
  });

  it('turns read-only when the room ends; a later visitor reads the ended notes', async () => {
    const host = await Host.create();
    const A = guest(host, 'Ana');
    await settle(host, A);
    append(A, 'final words\n');
    A.flush();
    await settle(host, A);
    host.end();
    await settle(host, A);
    expect(A.status).toBe('ended');
    expect(A.readOnly).toBe(true);
    expect([A.endedAt, A.expires]).toEqual([1234, 5678]);
    const sent = A.sentFrames;
    expect(A.applyLocal((doc) => insertText(doc, 0, 'nope'))).toBe(undefined);
    A.localChange();
    A.flush();
    await settle(host, A);
    expect(A.sentFrames).toBe(sent);
    expect(text(A)).toBe('final words\n');

    const B = guest(host, 'Ben');
    await settle(host, A, B);
    expect(B.status).toBe('ended');
    expect(B.readOnly).toBe(true);
    expect(text(B)).toBe('final words\n');
  });

  it('shows who is here and who is typing; presence expires', async () => {
    let now = 1_000_000;
    const host = await Host.create();
    const A = guest(host, 'Ana');
    const B = guest(host, 'Ben', { now: () => now });
    await settle(host, A, B);
    // everyone announces themselves on joining
    expect(B.others.map((p) => p.name)).toEqual(['Ana']);
    expect(B.present).toBe(2);
    A.setTyping(true);
    await settle(host, A, B);
    expect(B.typing).toEqual(['Ana']);
    A.setTyping(false);
    await settle(host, A, B);
    expect(B.typing).toEqual([]);
    // the owner's bridge, gid ""
    const env = await sealJson(host.keys, host.room, Kind.PRESENCE, { gid: '', name: 'Olga', typing: false });
    for (const s of host.live) s.deliver(tagged(0x02, null, env));
    await settle(host, A, B);
    expect(B.others.map((p) => p.name)).toEqual(['Olga', 'Ana']);
    now += PRESENCE_TTL_MS + 1;
    expect(B.others).toEqual([]);
  });

  it('sends a moved selection at most every 150 ms, the newest one after the wait', async () => {
    const host = await Host.create();
    const A = guest(host, 'Ana');
    const B = guest(host, 'Ben');
    await settle(host, A, B);
    await tick(SEL_EVERY_MS + 20);
    const sels = async () => (await host.presencesFrom('g-Ana')).map((p) => p.sel);
    const base = (await sels()).length;
    const s1 = { a: 'a'.repeat(64), f: 'b'.repeat(64) };
    const s2 = { a: 'c'.repeat(64), f: null };
    const s3 = { a: 'd'.repeat(64), f: 'd'.repeat(64) };
    A.setSel(s1);
    A.setSel(s2);
    A.setSel(s3);
    await tick(20);
    // the first goes out at once, the rest wait
    expect((await sels()).slice(base)).toEqual([s1]);
    await tick(SEL_EVERY_MS + 60);
    expect((await sels()).slice(base)).toEqual([s1, s3]); // s2 was overtaken
    // a repeat sends nothing
    A.setSel({ ...s3 });
    await tick(SEL_EVERY_MS + 60);
    expect((await sels()).slice(base)).toEqual([s1, s3]);
    // and the others see it, with the name and typing they already had
    await settle(host, A, B);
    expect(B.others.find((p) => p.gid === 'g-Ana')?.sel).toEqual(s3);
    A.setSel(null);
    await tick(SEL_EVERY_MS + 60);
    await settle(host, A, B);
    expect((await sels()).at(-1)).toBe(null);
    expect(B.others.find((p) => p.gid === 'g-Ana')?.sel).toBe(null);
  });

  it('sends no selection while not live; the join and the heartbeat carry the current one', async () => {
    const host = await Host.create();
    const A = guest(host, 'Ana');
    const B = guest(host, 'Ben');
    await settle(host, A, B);
    const sel = { a: '1'.repeat(64), f: '2'.repeat(64) };
    socketOf(host, A).drop(1006);
    await tick(5);
    expect(A.status).toBe('reconnecting');
    const before = (await host.presencesFrom('g-Ana')).length;
    A.setSel(sel);
    await tick(SEL_EVERY_MS + 60);
    expect((await host.presencesFrom('g-Ana')).length).toBe(before);
    A.reconnect();
    await settle(host, A, B);
    const joined = await host.presencesFrom('g-Ana');
    expect(joined.at(-1)?.sel).toEqual(sel);
    expect(B.others.find((p) => p.gid === 'g-Ana')?.sel).toEqual(sel);
    // the 15 s heartbeat (called directly: it is a plain interval)
    const n = joined.length;
    (A as unknown as { presenceTick(): void }).presenceTick();
    await settle(host, A, B);
    const beat = await host.presencesFrom('g-Ana');
    expect(beat.length).toBe(n + 1);
    expect(beat.at(-1)).toEqual({ gid: 'g-Ana', name: 'Ana', typing: false, sel });
  });

  it('tells the room when the page goes away, and a gone drops that person at once', async () => {
    const host = await Host.create();
    const A = guest(host, 'Ana');
    const B = guest(host, 'Ben');
    await settle(host, A, B);
    expect(B.others.map((p) => p.name)).toEqual(['Ana']);
    A.pageHidden();
    await settle(host, A, B);
    expect((await host.presencesFrom('g-Ana')).at(-1)).toEqual({ gid: 'g-Ana', name: 'Ana', gone: true });
    expect(B.others).toEqual([]);
    // the owner's side too
    await host.fromOwner({ gid: 'o:k1', name: 'Olga', typing: false, sel: null });
    await settle(host, A, B);
    expect(B.others.map((p) => p.gid)).toEqual(['o:k1']);
    await host.fromOwner({ gid: 'o:k1', name: 'Olga', gone: true });
    await settle(host, A, B);
    expect(B.others).toEqual([]);
  });

  it("keeps someone's last valid selection and ignores an invalid one", async () => {
    const host = await Host.create();
    const B = guest(host, 'Ben');
    await settle(host, B);
    const good = { a: 'e'.repeat(64), f: null };
    const others = () => B.others.find((p) => p.gid === 'o:k1');
    await host.fromOwner({ gid: 'o:k1', name: 'Olga', typing: false, sel: good });
    await settle(host, B);
    expect(others()?.sel).toEqual(good);
    for (const bad of [{ a: 'xyz', f: null }, { a: 'e'.repeat(64) }, 'nope', [], { a: 5, f: null }, { a: 'e'.repeat(63), f: null }]) {
      await host.fromOwner({ gid: 'o:k1', name: 'Olga', typing: false, sel: bad });
      await settle(host, B);
      expect(others()?.sel).toEqual(good);
    }
    // no `sel` at all (an older page, the heartbeat of a host): kept
    await host.fromOwner({ gid: 'o:k1', name: 'Olga', typing: true });
    await settle(host, B);
    expect(others()).toMatchObject({ typing: true, sel: good });
    // null clears; uppercase hex is lowered
    await host.fromOwner({ gid: 'o:k1', name: 'Olga', typing: false, sel: { a: 'E'.repeat(64), f: null } });
    await settle(host, B);
    expect(others()?.sel).toEqual(good);
    await host.fromOwner({ gid: 'o:k1', name: 'Olga', typing: false, sel: null });
    await settle(host, B);
    expect(others()?.sel).toBe(null);
  });

  it('cleanSel accepts null and {a, f} of ids or null only', () => {
    expect(cleanSel(null)).toEqual({ ok: true, sel: null });
    expect(cleanSel({ a: null, f: null })).toEqual({ ok: true, sel: { a: null, f: null } });
    expect(cleanSel({ a: 'A'.repeat(64), f: 'b'.repeat(64) })).toEqual({ ok: true, sel: { a: 'a'.repeat(64), f: 'b'.repeat(64) } });
    expect(cleanSel(undefined).ok).toBe(false);
    expect(cleanSel({ a: null }).ok).toBe(false);
    expect(cleanSel({ a: 'zz', f: null }).ok).toBe(false);
  });

  it("counts owner-side people as typing, once by name, and draws everyone's caret", async () => {
    const host = await Host.create();
    const B = guest(host, 'Ben');
    await settle(host, B);
    const sel = { a: 'f'.repeat(64), f: 'f'.repeat(64) };
    await host.fromOwner({ gid: '', name: 'Corey', typing: false });
    await host.fromOwner({ gid: 'o:k1', name: 'Corey', typing: true, sel });
    await host.fromOwner({ gid: 'o:k2', name: 'Corey', typing: true });
    await host.fromOwner({ gid: 'o:k3', name: 'Ana', typing: false, sel: null });
    await settle(host, B);
    expect(B.typing).toEqual(['Corey']);
    const presence = createGuestPresence(B);
    // An owner-side entry's colour comes from the NAME, so two windows of one person agree.
    expect(presence.carets()).toEqual([{ key: 'o:k1', name: 'Corey', who: 'Corey', sel }]);
    // it hears only a change in the carets, not a heartbeat
    let heard = 0;
    const off = presence.subscribe(() => heard++);
    await host.fromOwner({ gid: 'o:k3', name: 'Ana', typing: true });
    await settle(host, B);
    expect(heard).toBe(0);
    await host.fromOwner({ gid: 'o:k3', name: 'Ana', typing: true, sel });
    await settle(host, B);
    expect(heard).toBe(1);
    off();
  });

  it('applies owner frames, threads, and skips envelopes it cannot open', async () => {
    const host = await Host.create();
    const A = guest(host, 'Ana');
    await settle(host, A);
    // the owner types: a frame from another store, gid ""
    const owner = new WasmHashWeb();
    const doc = { web: owner, obj: owner.createSeq(ORIGIN) };
    insertText(doc, 0, 'owner here\n');
    const delta = owner.takeDeltas();
    host.append(await sealNotes(host.keys, host.room, '', 'Olga', delta), null);
    host.append(
      await sealJson(host.keys, host.room, Kind.COMMENT, {
        op: 'threads',
        threads: [{ root: { id: 'abc', author: 'Olga', text: 'see this' }, replies: [{ id: 'def', parent_id: 'abc', author: 'Ana (guest)', gid: 'g-Ana' }] }]
      }),
      null
    );
    // sealed for another room: ignored
    const other = await Host.create();
    host.append(await sealNotes(other.keys, other.room, '', 'x', delta), null);
    await settle(host, A);
    expect(text(A)).toBe('owner here\n');
    expect(A.lastEdit?.name).toBe('Olga');
    expect(A.threads[0].root.text).toBe('see this');
    expect(A.threads[0].replies[0].gid).toBe('g-Ana');
    expect(A.skipped).toBe(1);
    // a comment op goes out sealed, with our gid and name
    expect(await A.sendComment({ op: 'create', id: '0123456789ab', text: 'hello' })).toBe(true);
    await settle(host, A);
    const last = host.log[host.log.length - 1];
    const { kind, plaintext } = await unseal(host.keys, host.room, last.env);
    expect(kind).toBe(Kind.COMMENT);
    expect(JSON.parse(new TextDecoder().decode(plaintext))).toEqual({
      op: 'create',
      id: '0123456789ab',
      text: 'hello',
      gid: 'g-Ana',
      name: 'Ana'
    });
  });

  it('synced resolves once the room acknowledged what was typed', async () => {
    const host = await Host.create();
    const A = guest(host, 'Ana');
    await settle(host, A);
    append(A, 'mark me\n');
    await A.synced();
    expect((await host.framesFrom('g-Ana')).length).toBe(1);
    expect(A.room.since).toBe(host.seq);
  });
  it('a first visit reads the room without announcing itself or saying anything until it has a name', async () => {
    const host = await Host.create();
    const A = guest(host, 'Ana');
    const B = guest(host, 'Ben', { gid: 'g-new', name: '' });
    await settle(host, A, B);
    // B reads, but nobody hears of B and B cannot comment
    expect(B.status).toBe('live');
    expect(B.others.map((p) => p.name)).toEqual(['Ana']);
    expect(A.others).toEqual([]);
    expect(await B.sendComment({ op: 'create', id: '0123456789ab', text: 'hi' })).toBe(false);
    B.setName('Ben');
    await settle(host, A, B);
    expect(A.others.map((p) => p.name)).toEqual(['Ben']);
    expect(await B.sendComment({ op: 'create', id: '0123456789ab', text: 'hi' })).toBe(true);
  });

  it('counts the catch-ups, one for each (re)connect', async () => {
    const host = await Host.create();
    const A = guest(host, 'Ana');
    await settle(host, A);
    expect(A.catchUps).toBe(1);
    socketOf(host, A).drop();
    A.reconnect();
    await settle(host, A);
    expect(A.catchUps).toBe(2);
  });
});

// ---- the copy kept in the browser ------------------------------------------------------------

/** Sockets that close at once with `code` while `gate.down`, else behave like the host's. */
function gated(host: Host, gate: { down: boolean; code: number }): new (url: string) => HostSocket {
  return class extends host.Socket {
    constructor(url: string) {
      super(url);
      if (gate.down) {
        this.readyState = 3;
        queueMicrotask(() => this.onclose?.({ code: gate.code }));
      }
    }
  };
}

const FUTURE = () => new Date(Date.now() + 3_600_000).toISOString();

/** A room with some notes, a sealed snapshot of them, and the guest that wrote them. */
async function withSnapshot(): Promise<{ host: Host; env: Uint8Array }> {
  const host = await Host.create();
  const A = guest(host, 'Ana');
  await settle(host, A);
  append(A, 'kept notes\n');
  A.flush();
  await settle(host, A);
  await host.postSnapshot({
    notes: b64urlEncode(A.doc!.web.encode()),
    frames: [],
    origin: ORIGIN,
    meta: { title: 'Weekly', date: '2026-10-24', owner: 'Olga', agenda: [], ended: false, ended_at: '' }
  });
  A.destroy();
  return { host, env: host.snapshot!.env };
}

function savedRec(env: Uint8Array, over: Partial<CopyRec> = {}): CopyRec {
  return { envelope: env, expires: FUTURE(), ended_at: null, saved_at: '2026-10-24T10:00:00.000Z', ...over };
}

describe('GuestDoc saved copy', () => {
  it('keeps the latest snapshot envelope as received, with the room\'s times', async () => {
    const { host, env } = await withSnapshot();
    const copies = new MemoryCopyStore();
    const B = guest(host, 'Ben', { copies });
    await settle(host, B);
    const kept = copies.copies.get(host.room)!;
    expect(Array.from(kept.envelope)).toEqual(Array.from(env));
    expect(kept.expires).toBe(new Date(9999 * 1000).toISOString());
    expect(kept.ended_at).toBeNull();
    expect(Number.isNaN(Date.parse(kept.saved_at))).toBe(false);
    // nothing but envelope and times: no key, no plaintext
    expect(Object.keys(kept).sort()).toEqual(['ended_at', 'envelope', 'expires', 'saved_at']);
    // the room ending adds ended_at and the new expiry to the same envelope
    host.end();
    await settle(host, B);
    const ended = copies.copies.get(host.room)!;
    expect(Array.from(ended.envelope)).toEqual(Array.from(env));
    expect(ended.ended_at).toBe(new Date(1234 * 1000).toISOString());
    expect(ended.expires).toBe(new Date(5678 * 1000).toISOString());
  });

  it('keeps nothing when the room sent no snapshot', async () => {
    const host = await Host.create();
    const copies = new MemoryCopyStore();
    const A = guest(host, 'Ana', { copies });
    await settle(host, A);
    expect(copies.copies.size).toBe(0);
  });

  it('offline with a saved copy: opens it read-only, decrypted with the link\'s key', async () => {
    const { host, env } = await withSnapshot();
    const copies = new MemoryCopyStore();
    await copies.save(host.room, savedRec(env));
    const gate = { down: true, code: 1006 };
    const B = guest(host, 'Ben', { copies, webSocket: gated(host, gate), offlineMs: 20 });
    await settle(host, B);
    expect(B.status).toBe('saved');
    expect(B.saved).toBe(true);
    expect(B.readOnly).toBe(true);
    expect(B.savedAt).toBe(Date.parse('2026-10-24T10:00:00.000Z'));
    expect(text(B)).toBe('kept notes\n');
    expect(B.meta?.owner).toBe('Olga');
    // read-only: no edits, no comments
    expect(B.applyLocal((d) => insertText(d, 0, 'x'))).toBeUndefined();
    expect(await B.sendComment({ op: 'create' })).toBe(false);
    expect(text(B)).toBe('kept notes\n');
    expect(copies.copies.has(host.room)).toBe(true);
  });

  it('opens the saved copy after the offline budget when no socket ever answers', async () => {
    const { host, env } = await withSnapshot();
    const copies = new MemoryCopyStore();
    await copies.save(host.room, savedRec(env));
    // a socket that opens but never welcomes
    const silent = class extends host.Socket {
      constructor(url: string) {
        super(url);
        this.send = () => {};
      }
    };
    const B = guest(host, 'Ben', { copies, webSocket: silent, offlineMs: 20 });
    await tick(5);
    expect(B.status).toBe('connecting');
    await tick(40);
    await settle(host, B);
    expect(B.status).toBe('saved');
    expect(text(B)).toBe('kept notes\n');
  });

  it('removes an expired copy and shows nothing', async () => {
    const { host, env } = await withSnapshot();
    const copies = new MemoryCopyStore();
    await copies.save(host.room, savedRec(env, { expires: new Date(Date.now() - 1000).toISOString() }));
    const B = guest(host, 'Ben', { copies, webSocket: gated(host, { down: true, code: 1006 }), offlineMs: 20 });
    await settle(host, B);
    expect(B.status).toBe('connecting');
    expect(B.doc).toBeNull();
    expect(copies.copies.size).toBe(0);
  });

  it('removes the copy when the room is gone (4410)', async () => {
    const { host, env } = await withSnapshot();
    // offline start: the host answers 4410 at once
    const a = new MemoryCopyStore();
    await a.save(host.room, savedRec(env));
    const B = guest(host, 'Ben', { copies: a, webSocket: gated(host, { down: true, code: 4410 }), offlineMs: 20 });
    await settle(host, B);
    expect(B.status).toBe('gone');
    expect(B.saved).toBe(false);
    expect(a.copies.size).toBe(0);
    // and after having been live: the host drops the room
    const b = new MemoryCopyStore();
    const C = guest(host, 'Cy', { copies: b });
    await settle(host, C);
    expect(b.copies.size).toBe(1);
    socketOf(host, C).drop(4410);
    await settle(host, C);
    expect(C.status).toBe('gone');
    expect(b.copies.size).toBe(0);
  });

  it('removes a copy the link\'s key does not open', async () => {
    const { host } = await withSnapshot();
    const other = await Host.create();
    await other.postSnapshot({ notes: '', frames: [], origin: ORIGIN, meta: { title: 'Other', owner: 'Eve' } });
    const copies = new MemoryCopyStore();
    await copies.save(host.room, savedRec(other.snapshot!.env));
    const B = guest(host, 'Ben', { copies, webSocket: gated(host, { down: true, code: 1006 }), offlineMs: 20 });
    await settle(host, B);
    expect(B.status).toBe('connecting');
    expect(B.saved).toBe(false);
    expect(B.doc).toBeNull();
    expect(copies.copies.size).toBe(0);
  });

  it('a live connection replaces the saved copy', async () => {
    const { host, env } = await withSnapshot();
    const copies = new MemoryCopyStore();
    await copies.save(host.room, savedRec(env));
    const gate = { down: true, code: 1006 };
    const B = guest(host, 'Ben', { copies, webSocket: gated(host, gate), offlineMs: 20 });
    await settle(host, B);
    expect(B.status).toBe('saved');
    // the owner's computer comes back with newer notes
    const owner = host.seq;
    gate.down = false;
    host.append(
      await sealNotes(host.keys, host.room, '', 'Olga', (() => {
        const w = new WasmHashWeb();
        w.mergeEncoded(B.doc!.web.encode());
        const d = openDoc(w, ORIGIN);
        insertText(d, d.web.textLen(d.obj), 'newer\n');
        return drain(w)[0];
      })()),
      null
    );
    expect(host.seq).toBe(owner + 1);
    B.reconnect();
    await settle(host, B);
    expect(B.status).toBe('live');
    expect(B.saved).toBe(false);
    expect(B.readOnly).toBe(false);
    expect(text(B)).toContain('kept notes\n');
    expect(text(B)).toContain('newer\n');
    expect(B.applyLocal((d) => insertText(d, 0, 'x'))).not.toBeUndefined();
    expect(copies.copies.has(host.room)).toBe(true);
  });
});

describe('META clock', () => {
  const ok = { part: 2, offset: 30.5, started_at: '2026-10-25T10:00:00Z' };
  it('parses a valid clock', () => {
    expect(parseMeta({ title: 'T', clock: ok })?.clock).toEqual(ok);
  });
  it('treats an invalid clock as absent', () => {
    for (const bad of [
      { ...ok, part: -1 },
      { ...ok, part: 1.5 },
      { ...ok, offset: -1 },
      { ...ok, offset: Infinity },
      { ...ok, started_at: 'nope' },
      { ...ok, started_at: 5 },
      'x',
      null
    ]) {
      expect(parseMeta({ title: 'T', clock: bad })?.clock).toBeUndefined();
    }
  });
  it('is absent when not sent', () => {
    expect(parseMeta({ title: 'T' })).not.toHaveProperty('clock');
  });
});
