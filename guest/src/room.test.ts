// RoomClient against a scripted fake WebSocket: the host's side is played by the test.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  DEAD_MS,
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
