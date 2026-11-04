// RoomClient against a scripted fake WebSocket: the host's side is played by the test.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  DEAD_MS,
  MAX_GETS,
  MediaError,
  PING_MS,
  RECONNECT_MS,
  ROOM_CLOSE,
  RoomClient,
  roomUrl,
  type RoomHandlers,
  type RoomSocket,
  type RoomState
} from './room';

class FakeSocket implements RoomSocket {
  static all: FakeSocket[] = [];
  readyState = 0;
  binaryType = '';
  sent: (string | Uint8Array)[] = [];
  onopen: RoomSocket['onopen'] = null;
  onmessage: RoomSocket['onmessage'] = null;
  onclose: RoomSocket['onclose'] = null;
  onerror: RoomSocket['onerror'] = null;
  closedByClient: number | null = null;

  constructor(public url: string) {
    FakeSocket.all.push(this);
  }
  send(data: string | Uint8Array): void {
    if (this.readyState !== 1) throw new Error('socket not open');
    this.sent.push(data);
  }
  close(code = 1000): void {
    if (this.readyState >= 2) return;
    this.readyState = 3;
    this.closedByClient = code;
    queueMicrotask(() => this.onclose?.({ code }));
  }
  // host side
  accept(): void {
    this.readyState = 1;
    this.onopen?.({});
  }
  json(msg: object): void {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
  text(t: string): void {
    this.onmessage?.({ data: t });
  }
  bin(b: Uint8Array): void {
    this.onmessage?.({ data: b.slice().buffer });
  }
  serverClose(code: number): void {
    this.readyState = 3;
    this.onclose?.({ code });
  }
  get join(): { type: string; token: string; since: number } {
    return JSON.parse(this.sent[0] as string);
  }
  get binaries(): Uint8Array[] {
    return this.sent.filter((s): s is Uint8Array => s instanceof Uint8Array);
  }
}

function last(): FakeSocket {
  return FakeSocket.all[FakeSocket.all.length - 1];
}

function tagged(tag: number, seq: number | null, env: Uint8Array): Uint8Array {
  const head = seq === null ? 1 : 9;
  const out = new Uint8Array(head + env.length);
  out[0] = tag;
  if (seq !== null) new DataView(out.buffer).setBigUint64(1, BigInt(seq), false);
  out.set(env, head);
  return out;
}

const env = (n: number) => new Uint8Array([n, n, n]);

function welcome(over: Partial<Record<string, unknown>> = {}) {
  return { type: 'welcome', seq: 0, snapshot_seq: null, ended: false, ended_at: null, expires: 99, present: 1, ...over };
}

interface Log {
  events: string[];
  states: RoomState[];
}

function make(opts: { probe?: () => Promise<'gone' | 'full' | null> } = {}): { room: RoomClient; log: Log } {
  const log: Log = { events: [], states: [] };
  const h: RoomHandlers = {
    onWelcome: (w) => log.events.push(`welcome:${w.seq}`),
    onSnapshot: (seq, e) => log.events.push(`snapshot:${seq}:${e[0]}`),
    onFrame: (seq, e) => log.events.push(`frame:${seq}:${e[0]}`),
    onPresence: (e) => log.events.push(`presence:${e[0]}`),
    onCount: (n) => log.events.push(`count:${n}`),
    onAck: (seq, e) => log.events.push(`ack:${seq}:${e ? e[0] : 'null'}`),
    onCaughtUp: () => log.events.push('caught_up'),
    onEnded: (at, exp) => log.events.push(`ended:${at}:${exp}`),
    onError: (code) => log.events.push(`error:${code}`),
    onMedia: (name) => log.events.push(`media:${name.slice(0, 4)}`),
    onState: (s) => log.states.push(s)
  };
  const room = new RoomClient(h, { webSocket: FakeSocket, probe: opts.probe });
  return { room, log };
}

beforeEach(() => {
  FakeSocket.all = [];
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe('roomUrl', () => {
  it('follows the page origin', () => {
    expect(roomUrl('0123456789abcdef', { protocol: 'https:', host: 'h.example' })).toBe(
      'wss://h.example/live/0123456789abcdef'
    );
    expect(roomUrl('0123456789abcdef', { protocol: 'http:', host: '127.0.0.1:8028' })).toBe(
      'ws://127.0.0.1:8028/live/0123456789abcdef'
    );
  });
});

describe('RoomClient', () => {
  it('joins, then gets the snapshot and the replay before it is caught up', () => {
    const { room, log } = make();
    room.connect('ws://h/live/r', 'tok');
    expect(room.state).toBe('connecting');
    last().accept();
    expect(last().join).toEqual({ type: 'join', token: 'tok', since: 0 });
    last().json(welcome({ seq: 5, snapshot_seq: 3, present: 2 }));
    expect(room.state).toBe('live');
    expect(room.present).toBe(2);
    last().bin(tagged(0x03, 3, env(7)));
    last().bin(tagged(0x01, 4, env(8)));
    expect(log.events).not.toContain('caught_up');
    last().bin(tagged(0x01, 5, env(9)));
    expect(log.events).toEqual(['welcome:5', 'snapshot:3:7', 'frame:4:8', 'frame:5:9', 'caught_up']);
    expect(room.since).toBe(5);
    expect(room.caughtUp).toBe(true);
    // live traffic
    last().bin(tagged(0x02, null, env(1)));
    last().json({ type: 'count', present: 3 });
    last().bin(tagged(0x01, 6, env(2)));
    expect(log.events.slice(5)).toEqual(['presence:1', 'count:3', 'frame:6:2']);
    expect(room.since).toBe(6);
  });

  it('is caught up at once when there is nothing to replay', () => {
    const { room, log } = make();
    room.connect('ws://h/live/r', 'tok');
    last().accept();
    last().json(welcome({ seq: 0 }));
    expect(log.events).toEqual(['welcome:0', 'caught_up']);
    // a snapshot covering everything, no frames after it
    const b = make();
    b.room.connect('ws://h/live/r', 'tok');
    last().accept();
    last().json(welcome({ seq: 4, snapshot_seq: 4 }));
    expect(b.log.events).toEqual(['welcome:4']);
    last().bin(tagged(0x03, 4, env(1)));
    expect(b.log.events).toEqual(['welcome:4', 'snapshot:4:1', 'caught_up']);
  });

  it('sends frames and presence tagged, and matches acks to frames in order', () => {
    const { room, log } = make();
    room.connect('ws://h/live/r', 'tok');
    last().accept();
    last().json(welcome());
    expect(room.sendFrame(env(1))).toBe(true);
    expect(room.sendFrame(env(2))).toBe(true);
    expect(room.sendPresence(env(3))).toBe(true);
    expect(last().binaries.map((b) => Array.from(b))).toEqual([
      [1, 1, 1, 1],
      [1, 2, 2, 2],
      [2, 3, 3, 3]
    ]);
    last().json({ type: 'ack', seq: 10 });
    last().json({ type: 'ack', seq: 12 });
    expect(log.events.slice(-2)).toEqual(['ack:10:1', 'ack:12:2']);
    expect(room.since).toBe(12);
  });

  it('reconnects with since after a drop, with backoff, and flushes what was queued', async () => {
    const { room, log } = make();
    room.connect('ws://h/live/r', 'tok');
    last().accept();
    last().json(welcome({ seq: 2 }));
    last().bin(tagged(0x01, 1, env(1)));
    last().bin(tagged(0x01, 2, env(2)));
    room.sendFrame(env(5));
    last().json({ type: 'ack', seq: 3 });
    expect(room.since).toBe(3);
    last().serverClose(1006);
    expect(room.state).toBe('reconnecting');
    // queued while down; a later presence replaces an earlier one
    expect(room.sendFrame(env(6))).toBe(false);
    expect(room.sendFrame(env(7), { queue: false })).toBe(false);
    room.sendPresence(env(8));
    room.sendPresence(env(9));
    expect(FakeSocket.all.length).toBe(1);
    await vi.advanceTimersByTimeAsync(RECONNECT_MS[0] - 1);
    expect(FakeSocket.all.length).toBe(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(FakeSocket.all.length).toBe(2);
    // a failed attempt waits longer
    last().serverClose(1006);
    await vi.advanceTimersByTimeAsync(RECONNECT_MS[1] - 1);
    expect(FakeSocket.all.length).toBe(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(FakeSocket.all.length).toBe(3);
    last().accept();
    expect(last().join.since).toBe(3);
    expect(last().binaries.length).toBe(0); // nothing before the welcome
    last().json(welcome({ seq: 3 }));
    expect(room.state).toBe('live');
    expect(last().binaries.map((b) => Array.from(b))).toEqual([
      [1, 6, 6, 6],
      [2, 9, 9, 9]
    ]);
    expect(log.events.filter((e) => e === 'caught_up').length).toBe(2);
  });

  it('a lagged close rejoins at once with since', async () => {
    const { room } = make();
    room.connect('ws://h/live/r', 'tok');
    last().accept();
    last().json(welcome({ seq: 1 }));
    last().bin(tagged(0x01, 1, env(1)));
    last().serverClose(ROOM_CLOSE.lagged);
    expect(room.state).toBe('reconnecting');
    await vi.advanceTimersByTimeAsync(0);
    expect(FakeSocket.all.length).toBe(2);
    last().accept();
    expect(last().join.since).toBe(1);
  });

  it('4410 and 4401 are gone: no reconnect', async () => {
    for (const code of [ROOM_CLOSE.gone, ROOM_CLOSE.unauthorized]) {
      FakeSocket.all = [];
      const { room } = make();
      room.connect('ws://h/live/r', 'tok');
      last().accept();
      last().json(welcome());
      last().serverClose(code);
      expect(room.state).toBe('gone');
      expect(room.sendFrame(env(1))).toBe(false);
      await vi.advanceTimersByTimeAsync(60_000);
      expect(FakeSocket.all.length).toBe(1);
    }
  });

  it('4410 straight after the upgrade (never joined) is gone: no reconnect', async () => {
    const { room } = make();
    room.connect('ws://h/live/r', 'tok');
    last().accept();
    last().serverClose(ROOM_CLOSE.gone);
    expect(room.state).toBe('gone');
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeSocket.all.length).toBe(1);
  });

  it('4429 is full: retried with the normal backoff, then joins when there is room', async () => {
    const { room } = make();
    room.connect('ws://h/live/r', 'tok');
    last().accept();
    last().serverClose(ROOM_CLOSE.full);
    expect(room.state).toBe('full');
    await vi.advanceTimersByTimeAsync(RECONNECT_MS[0] - 1);
    expect(FakeSocket.all.length).toBe(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(FakeSocket.all.length).toBe(2);
    last().accept();
    last().serverClose(ROOM_CLOSE.full);
    expect(room.state).toBe('full');
    await vi.advanceTimersByTimeAsync(RECONNECT_MS[1] - 1);
    expect(FakeSocket.all.length).toBe(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(FakeSocket.all.length).toBe(3);
    last().accept();
    last().json(welcome());
    expect(room.state).toBe('live');
  });

  it('4403 is ended: read-only, no reconnect', async () => {
    const { room, log } = make();
    room.connect('ws://h/live/r', 'tok');
    last().accept();
    last().json(welcome());
    last().serverClose(ROOM_CLOSE.ended);
    expect(room.state).toBe('ended');
    expect(room.ended).toBe(true);
    expect(log.events).toContain('ended:null:99'); // the welcome's expiry is kept
    expect(room.sendFrame(env(1))).toBe(false);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeSocket.all.length).toBe(1);
  });

  it('an ended event or an ended welcome turns it read-only and keeps the socket', async () => {
    const { room, log } = make();
    room.connect('ws://h/live/r', 'tok');
    last().accept();
    last().json(welcome());
    last().json({ type: 'ended', ended_at: 100, expires: 200 });
    expect(room.state).toBe('ended');
    expect([room.endedAt, room.expires]).toEqual([100, 200]);
    expect(log.events).toContain('ended:100:200');
    expect(room.sendFrame(env(1))).toBe(false);
    expect(last().closedByClient).toBe(null);
    // a later close of an ended room is not retried
    last().serverClose(1001);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeSocket.all.length).toBe(1);
    expect(room.state).toBe('ended');

    const b = make();
    b.room.connect('ws://h/live/r', 'tok');
    last().accept();
    last().json(welcome({ ended: true, ended_at: 5, expires: 9, seq: 1 }));
    expect(b.room.state).toBe('ended');
    expect(b.log.events).toEqual(['welcome:1', 'ended:5:9']);
    last().bin(tagged(0x01, 1, env(4)));
    expect(b.log.events).toEqual(['welcome:1', 'ended:5:9', 'frame:1:4', 'caught_up']);
  });

  it('reports host errors', () => {
    const { room, log } = make();
    room.connect('ws://h/live/r', 'tok');
    last().accept();
    last().json(welcome());
    last().json({ type: 'error', code: 'log_full' });
    expect(log.events).toContain('error:log_full');
    expect(room.lastError).toBe('log_full');
  });

  it('pings, and treats a silent socket as dead', async () => {
    const { room } = make();
    room.connect('ws://h/live/r', 'tok');
    last().accept();
    last().json(welcome());
    await vi.advanceTimersByTimeAsync(PING_MS);
    expect(last().sent).toContain('ping');
    last().text('pong');
    expect(room.state).toBe('live');
    // last heard at PING_MS; checked on the ping ticks (every PING_MS)
    await vi.advanceTimersByTimeAsync(DEAD_MS - 1);
    expect(room.state).toBe('live');
    const firstDeadTick = Math.ceil((PING_MS + DEAD_MS) / PING_MS) * PING_MS;
    await vi.advanceTimersByTimeAsync(firstDeadTick - (PING_MS + DEAD_MS - 1));
    expect(room.state).toBe('reconnecting');
    expect(FakeSocket.all[0].closedByClient).not.toBe(null);
    await vi.advanceTimersByTimeAsync(RECONNECT_MS[0]);
    expect(FakeSocket.all.length).toBe(2);
  });

  it('a probe can tell gone or full after a failed upgrade', async () => {
    let answer: 'gone' | 'full' | null = 'full';
    const { room } = make({ probe: async () => answer });
    room.connect('ws://h/live/r', 'tok');
    last().serverClose(1006); // never opened
    await vi.advanceTimersByTimeAsync(0);
    expect(room.state).toBe('full');
    await vi.advanceTimersByTimeAsync(RECONNECT_MS[0]);
    expect(FakeSocket.all.length).toBe(2);
    answer = 'gone';
    last().serverClose(1006);
    await vi.advanceTimersByTimeAsync(0);
    expect(room.state).toBe('gone');
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeSocket.all.length).toBe(2);
  });

  it('close stops for good', async () => {
    const { room } = make();
    room.connect('ws://h/live/r', 'tok');
    last().accept();
    last().json(welcome());
    room.close();
    expect(room.state).toBe('closed');
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeSocket.all.length).toBe(1);
  });
});


describe('RoomClient media', () => {
  const hex = (n: number) => n.toString(16).padStart(2, '0').repeat(32);
  const raw = (n: number) => new Uint8Array(32).fill(n);
  const blobEnv = new Uint8Array([6, 1, 2, 3, 4]);

  function joined() {
    const m = make();
    m.room.connect('ws://h/live/r', 'tok');
    last().accept();
    last().json(welcome());
    return m;
  }
  const gets = (s: FakeSocket) =>
    s.sent
      .filter((x): x is string => typeof x === 'string' && x.startsWith('{'))
      .map((x) => JSON.parse(x))
      .filter((x) => x.type === 'get_media')
      .map((x) => x.name as string);
  const puts = (s: FakeSocket) => s.binaries.filter((b) => b[0] === 0x04);
  const settled = async (p: Promise<unknown>) => p.then(() => 'ok', (e) => (e instanceof MediaError ? e.code : String(e)));

  it('puts 0x04 || name || envelope and resolves on the ok answer', async () => {
    const { room } = joined();
    const done = room.putMedia(hex(0xab), blobEnv);
    const [out] = puts(last());
    expect(out.length).toBe(33 + blobEnv.length);
    expect(Array.from(out.subarray(1, 33))).toEqual(Array.from(raw(0xab)));
    expect(Array.from(out.subarray(33))).toEqual(Array.from(blobEnv));
    last().json({ type: 'media', name: hex(0xcd), ok: true }); // another name: nothing
    last().json({ type: 'media', name: hex(0xab), ok: true });
    await expect(done).resolves.toBeUndefined();
    // answered: not sent again after a reconnect
    last().serverClose(1006);
    vi.advanceTimersByTime(RECONNECT_MS[0]);
    last().accept();
    last().json(welcome());
    expect(puts(last())).toHaveLength(0);
  });

  it('rejects a put on the host error that names it, and keeps the socket', async () => {
    const { room } = joined();
    const a = settled(room.putMedia(hex(1), blobEnv));
    const b = settled(room.putMedia(hex(2), blobEnv));
    const c = settled(room.putMedia(hex(3), blobEnv));
    last().json({ type: 'error', code: 'media_too_large', name: hex(1) });
    last().json({ type: 'error', code: 'room_media_full', name: hex(2) });
    last().json({ type: 'media', name: hex(3), ok: true });
    expect(await Promise.all([a, b, c])).toEqual(['media_too_large', 'room_media_full', 'ok']);
    expect(room.state).toBe('live');
  });

  it('keeps a put made while not joined and sends it after the welcome, again after a reconnect', async () => {
    const { room } = make();
    room.connect('ws://h/live/r', 'tok');
    const done = room.putMedia(hex(5), blobEnv); // before the socket is open
    last().accept();
    expect(puts(last())).toHaveLength(0);
    last().json(welcome());
    expect(puts(last())).toHaveLength(1);
    // the socket dies before the answer: the next welcome sends it again
    last().serverClose(1006);
    vi.advanceTimersByTime(RECONNECT_MS[0]);
    last().accept();
    last().json(welcome());
    expect(puts(last())).toHaveLength(1);
    last().json({ type: 'media', name: hex(5), ok: true });
    await expect(done).resolves.toBeUndefined();
  });

  it('gets a blob: text get_media out, 0x05 || name || envelope in', async () => {
    const { room } = joined();
    const got = room.getMedia(hex(9));
    expect(gets(last())).toEqual([hex(9)]);
    last().bin(tagged(0x05, null, new Uint8Array([...raw(0x08), 7, 7]))); // someone else's name: ignored
    last().bin(tagged(0x05, null, new Uint8Array([...raw(0x09), ...blobEnv])));
    expect(Array.from(await got)).toEqual(Array.from(blobEnv));
  });

  it('shares one request between asks of the same name; media_missing rejects them all', async () => {
    const { room } = joined();
    const a = settled(room.getMedia(hex(4)));
    const b = settled(room.getMedia(hex(4)));
    expect(gets(last())).toEqual([hex(4)]);
    last().json({ type: 'error', code: 'media_missing', name: hex(4) });
    expect(await Promise.all([a, b])).toEqual(['media_missing', 'media_missing']);
    // and may be asked again
    const again = room.getMedia(hex(4));
    expect(gets(last())).toEqual([hex(4), hex(4)]);
    last().bin(tagged(0x05, null, new Uint8Array([...raw(4), ...blobEnv])));
    await again;
  });

  it('has at most MAX_GETS asks in flight; each answer lets the next go', async () => {
    const { room } = joined();
    const all = Array.from({ length: MAX_GETS + 3 }, (_, i) => settled(room.getMedia(hex(i + 1))));
    expect(gets(last())).toHaveLength(MAX_GETS);
    last().json({ type: 'error', code: 'media_missing', name: hex(1) });
    expect(gets(last())).toHaveLength(MAX_GETS + 1);
    last().bin(tagged(0x05, null, new Uint8Array([...raw(2), 1])));
    last().bin(tagged(0x05, null, new Uint8Array([...raw(3), 1])));
    expect(gets(last())).toHaveLength(MAX_GETS + 3);
    expect(gets(last()).slice(-3)).toEqual([hex(21), hex(22), hex(23)]);
    for (let i = 4; i <= MAX_GETS + 3; i++) last().bin(tagged(0x05, null, new Uint8Array([...raw(i), 1])));
    const r = await Promise.all(all);
    expect(r[0]).toBe('media_missing');
    expect(r.slice(1)).toEqual(Array(MAX_GETS + 2).fill('ok'));
  });

  it('asks again after media_busy, and again for asks lost with the socket', async () => {
    const { room } = joined();
    const got = room.getMedia(hex(7));
    last().json({ type: 'error', code: 'media_busy', name: hex(7) });
    expect(gets(last())).toHaveLength(1);
    vi.advanceTimersByTime(300);
    expect(gets(last())).toHaveLength(2);
    last().serverClose(1006);
    vi.advanceTimersByTime(RECONNECT_MS[0]);
    last().accept();
    last().json(welcome());
    expect(gets(last())).toEqual([hex(7)]);
    last().bin(tagged(0x05, null, new Uint8Array([...raw(7), ...blobEnv])));
    expect(Array.from(await got)).toEqual(Array.from(blobEnv));
  });

  it('tells the handler about a blob that landed (a media message without ok)', () => {
    const { log } = joined();
    last().json({ type: 'media', name: hex(0xee) });
    last().json({ type: 'media', name: 'nonsense' });
    expect(log.events).toContain('media:eeee');
    expect(log.events.filter((e) => e.startsWith('media:'))).toHaveLength(1);
  });

  it('refuses a bad name, rejects pending asks when the room is closed or gone, and puts when it ended', async () => {
    const { room } = joined();
    expect(await settled(room.putMedia('xyz', blobEnv))).toBe('bad_name');
    expect(await settled(room.getMedia(hex(0xab).toUpperCase()))).toBe('bad_name');
    const get = settled(room.getMedia(hex(1)));
    const put = settled(room.putMedia(hex(2), blobEnv));
    last().json({ type: 'ended', ended_at: 5, expires: 9 });
    expect(await put).toBe('room_ended');
    expect(await settled(room.putMedia(hex(3), blobEnv))).toBe('room_ended');
    room.close();
    expect(await get).toBe('closed');
    expect(await settled(room.getMedia(hex(1)))).toBe('closed');

    const g = joined();
    const pending = settled(g.room.getMedia(hex(5)));
    last().serverClose(4410);
    expect(g.room.state).toBe('gone');
    expect(await pending).toBe('closed');
  });
});
