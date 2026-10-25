// The transport order: iroh to the owner's computer first (IROH_BUDGET_MS after the wasm is up),
// then the host's WebSocket when the page is served by one; the one that welcomes us is kept, and
// each keeps its own `since`.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fakeWasm, type FakeConn } from './fake-guest-wasm';
import { keyToFragment } from './format';
import type { GuestWasmLoader } from './iroh';
import { parseLink, type GuestLink } from './link';
import { RECONNECT_MS, RoomClient, type RoomSocket, type RoomState, type RoomTransport } from './room';
import { IROH_BUDGET_MS, transportsFor } from './transports';

class HostSocket implements RoomSocket {
  static all: HostSocket[] = [];
  readyState = 0;
  binaryType = '';
  sent: (string | Uint8Array)[] = [];
  onopen: RoomSocket['onopen'] = null;
  onmessage: RoomSocket['onmessage'] = null;
  onclose: RoomSocket['onclose'] = null;
  onerror: RoomSocket['onerror'] = null;
  constructor(public url: string) {
    HostSocket.all.push(this);
  }
  send(data: string | Uint8Array): void {
    if (this.readyState !== 1) throw new Error('socket not open');
    this.sent.push(data);
  }
  close(code = 1000): void {
    if (this.readyState >= 2) return;
    this.readyState = 3;
    queueMicrotask(() => this.onclose?.({ code }));
  }
  accept(): void {
    this.readyState = 1;
    this.onopen?.({});
  }
  json(msg: object): void {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
  serverClose(code: number): void {
    this.readyState = 3;
    this.onclose?.({ code });
  }
  get join(): { since: number } {
    return JSON.parse(this.sent[0] as string);
  }
}

const ROOM = '0123456789abcdef';
const KEY = keyToFragment(new Uint8Array(32).fill(7));
const EP = 'cd'.repeat(32);
const LOC = { protocol: 'https:', host: 'notes.example.com' };

function link(hostPath: boolean, relay = 'relay.example.com'): GuestLink {
  const l = parseLink({ pathname: hostPath ? `/n/${ROOM}` : '/mnemosyne/n/', hash: `#2.${ROOM}.${KEY}.${EP}.${relay}` });
  if (!l) throw new Error('bad test link');
  return l;
}

function welcome(over: Record<string, unknown> = {}) {
  return { type: 'welcome', seq: 0, snapshot_seq: null, ended: false, ended_at: null, expires: 99, present: 1, ...over };
}

function frame(seq: number): Uint8Array {
  const out = new Uint8Array(12);
  out[0] = 0x01;
  new DataView(out.buffer).setBigUint64(1, BigInt(seq), false);
  return out;
}

/** The join the owner's side received on this connection. */
function joinOf(conn: FakeConn): { since: number } {
  return JSON.parse(conn.texts[0]);
}

interface Run {
  room: RoomClient;
  states: RoomState[];
  kinds: string[];
  welcomes: number;
}

function start(transports: RoomTransport[]): Run {
  const run: Run = { room: null as unknown as RoomClient, states: [], kinds: [], welcomes: 0 };
  run.room = new RoomClient(
    {
      onWelcome: () => run.welcomes++,
      onState: (s) => run.states.push(s),
      onTransport: (k) => run.kinds.push(k)
    },
    { webSocket: HostSocket }
  );
  run.room.connect(transports, 'token');
  return run;
}

async function settle(): Promise<void> {
  for (let i = 0; i < 10; i++) await Promise.resolve();
}

beforeEach(() => {
  HostSocket.all = [];
  vi.useFakeTimers();
});
afterEach(() => vi.useRealTimers());

describe('transportsFor', () => {
  it('lists iroh, then the host only when the path carries the room', () => {
    const w = fakeWasm();
    const both = transportsFor(link(true), { loader: w.loader, loc: LOC });
    expect(both.map((t) => t.kind)).toEqual(['iroh', 'host']);
    expect(both[0].url).toBe(`iroh:${EP}?relay=${encodeURIComponent('https://relay.example.com')}`);
    expect(both[0].budgetMs).toBe(IROH_BUDGET_MS);
    expect(both[1].url).toBe(`wss://notes.example.com/live/${ROOM}`);
    expect(transportsFor(link(false), { loader: w.loader, loc: LOC }).map((t) => t.kind)).toEqual(['iroh']);
    const dev = transportsFor(link(false, '127.0.0.1:3340'), { loader: w.loader, loc: LOC });
    expect(dev[0].url).toContain(encodeURIComponent('http://127.0.0.1:3340'));
    const f1 = parseLink({ pathname: `/n/${ROOM}`, hash: `#${KEY}` })!;
    expect(transportsFor(f1, { loader: w.loader, loc: LOC }).map((t) => t.kind)).toEqual(['host']);
  });
});

describe('transport order', () => {
  it('iroh that works: the host is never opened', async () => {
    const w = fakeWasm();
    const run = start(transportsFor(link(true), { loader: w.loader, loc: LOC, webSocket: HostSocket }));
    expect(run.room.transport).toBe('iroh');
    await settle();
    const conn = w.conns[0];
    expect(joinOf(conn)).toMatchObject({ type: 'join', token: 'token', since: 0 });
    conn.text(JSON.stringify(welcome()));
    await settle();
    expect(run.welcomes).toBe(1);
    expect(run.room.state).toBe('live');
    await vi.advanceTimersByTimeAsync(IROH_BUDGET_MS * 3);
    expect(HostSocket.all).toHaveLength(0);
    expect(run.kinds).toEqual(['iroh']);

    // Dropped after the welcome: iroh again, never the host.
    conn.peerClose(1006, 'timed out');
    await settle();
    await vi.advanceTimersByTimeAsync(RECONNECT_MS[0]);
    await settle();
    expect(w.conns).toHaveLength(2);
    expect(HostSocket.all).toHaveLength(0);
    run.room.close();
  });

  it('iroh not welcomed within the budget (counted once the wasm is up): the host', async () => {
    const w = fakeWasm();
    w.outcome = 'hang';
    // The wasm takes 5 s to arrive; the 8 s start after that.
    const slow: GuestWasmLoader = () => new Promise((r) => setTimeout(r, 5000)).then(() => w.loader());
    const run = start(transportsFor(link(true), { loader: slow, loc: LOC, webSocket: HostSocket }));
    await vi.advanceTimersByTimeAsync(5000);
    expect(w.calls).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(IROH_BUDGET_MS - 1);
    expect(HostSocket.all).toHaveLength(0);
    expect(run.room.transport).toBe('iroh');
    await vi.advanceTimersByTimeAsync(1);
    expect(HostSocket.all).toHaveLength(1);
    expect(run.room.transport).toBe('host');
    expect(run.kinds).toEqual(['iroh', 'host']);
    expect(run.states).toEqual([]); // still 'connecting' throughout
    const host = HostSocket.all[0];
    host.accept();
    host.json(welcome());
    expect(run.room.state).toBe('live');

    // A connect that finally succeeds after giving up is closed, never used.
    w.pending[0]();
    await settle();
    expect(w.conns[0].closedWith).not.toBeNull();
    expect(w.conns[0].sent).toEqual([]);

    // Kept from now on: a drop reconnects to the host, no iroh attempt.
    host.serverClose(1006);
    await vi.advanceTimersByTimeAsync(RECONNECT_MS[0]);
    expect(HostSocket.all).toHaveLength(2);
    expect(w.calls).toHaveLength(1);
    run.room.close();
  });

  it('iroh that fails at once: the host at once, without waiting for the budget', async () => {
    const w = fakeWasm();
    w.outcome = 'reject';
    const run = start(transportsFor(link(true), { loader: w.loader, loc: LOC, webSocket: HostSocket }));
    await settle();
    expect(HostSocket.all).toHaveLength(1);
    expect(run.room.transport).toBe('host');
    run.room.close();
  });

  it('a wasm that does not load: the host', async () => {
    const broken: GuestWasmLoader = () => Promise.reject(new Error('blocked'));
    const run = start(transportsFor(link(true), { loader: broken, loc: LOC, webSocket: HostSocket }));
    await settle();
    expect(HostSocket.all).toHaveLength(1);
    run.room.close();
  });

  it('iroh alone has no budget: it keeps waiting', async () => {
    const w = fakeWasm();
    w.outcome = 'hang';
    const run = start(transportsFor(link(false), { loader: w.loader, loc: LOC, webSocket: HostSocket }));
    await vi.advanceTimersByTimeAsync(IROH_BUDGET_MS * 4);
    expect(w.calls).toHaveLength(1);
    expect(run.room.transport).toBe('iroh');
    w.pending[0]();
    await settle();
    w.conns[0].text(JSON.stringify(welcome()));
    await settle();
    expect(run.room.state).toBe('live');
    run.room.close();
  });

  it('both failing before a welcome: backoff, then a new round from iroh', async () => {
    const w = fakeWasm();
    w.outcome = 'reject';
    const run = start(transportsFor(link(true), { loader: w.loader, loc: LOC, webSocket: HostSocket }));
    await settle();
    HostSocket.all[0].serverClose(1006);
    expect(run.room.transport).toBe('iroh');
    await vi.advanceTimersByTimeAsync(RECONNECT_MS[0]);
    await settle();
    expect(w.calls).toHaveLength(2);
    expect(HostSocket.all).toHaveLength(2);
    run.room.close();
  });

  it("4410 from iroh before a welcome: the host decides; the host's 4410 too: gone", async () => {
    const w = fakeWasm();
    const run = start(transportsFor(link(true), { loader: w.loader, loc: LOC, webSocket: HostSocket }));
    await settle();
    w.conns[0].peerClose(4410, 'gone');
    await settle();
    expect(HostSocket.all).toHaveLength(1);
    expect(run.room.state).toBe('connecting');
    HostSocket.all[0].serverClose(4410);
    expect(run.room.state).toBe('gone');
  });

  it('4410 from the host while iroh only timed out: iroh is tried again', async () => {
    const w = fakeWasm();
    w.outcome = 'hang';
    const run = start(transportsFor(link(true), { loader: w.loader, loc: LOC, webSocket: HostSocket }));
    await vi.advanceTimersByTimeAsync(IROH_BUDGET_MS);
    HostSocket.all[0].serverClose(4410);
    expect(run.room.state).toBe('connecting');
    await vi.advanceTimersByTimeAsync(RECONNECT_MS[0]);
    expect(w.calls).toHaveLength(2);
    // The host is out for good: no budget now, iroh is all that is left.
    await vi.advanceTimersByTimeAsync(IROH_BUDGET_MS * 2);
    expect(HostSocket.all).toHaveLength(1);
    run.room.close();
  });

  it('keeps since per transport', async () => {
    const w = fakeWasm();
    w.outcome = 'hang';
    const ts = transportsFor(link(true), { loader: w.loader, loc: LOC, webSocket: HostSocket });
    ts[0].since = 5;
    ts[1].since = 9;
    const run = start(ts);
    expect(run.room.since).toBe(5);
    await settle();
    w.pending[0]();
    await settle();
    expect(joinOf(w.conns[0]).since).toBe(5);
    // Opened but no welcome within the budget: the host, with its own since.
    await vi.advanceTimersByTimeAsync(IROH_BUDGET_MS);
    const host = HostSocket.all[0];
    host.accept();
    expect(host.join.since).toBe(9);
    expect(run.room.since).toBe(9);
    host.json(welcome({ seq: 12 }));
    host.onmessage?.({ data: frame(12).buffer });
    expect(run.room.since).toBe(12);
    host.serverClose(1006);
    await vi.advanceTimersByTimeAsync(RECONNECT_MS[0]);
    expect(HostSocket.all[1].url).toBe(host.url);
    HostSocket.all[1].accept();
    expect(HostSocket.all[1].join.since).toBe(12);
    run.room.close();
  });

  it('a single URL still works as before', () => {
    const room = new RoomClient({}, { webSocket: HostSocket });
    room.connect('wss://h/live/x', 't', 4);
    expect(room.transport).toBe('host');
    HostSocket.all[0].accept();
    expect(HostSocket.all[0].join.since).toBe(4);
    room.close();
  });
});
