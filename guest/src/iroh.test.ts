// IrohSocket against a fake guest_wasm module: the owner's side is played by the test.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fakeWasm } from './fake-guest-wasm';
import { FRAME_BINARY, FRAME_TEXT, GUEST_ALPN, IrohSocket, guestWasm, irohUrl, parseIrohUrl, type GuestWasmLoader } from './iroh';

const EP = 'ab'.repeat(32);
const URL_ = irohUrl(EP, 'https://relay.example.com');

async function settle(): Promise<void> {
  for (let i = 0; i < 10; i++) await Promise.resolve();
}

interface Seen {
  events: string[];
  messages: unknown[];
  closes: { code: number; reason?: string }[];
}

function watch(s: IrohSocket): Seen {
  const seen: Seen = { events: [], messages: [], closes: [] };
  s.onopen = () => seen.events.push('open');
  s.onmessage = (ev) => {
    seen.events.push('message');
    seen.messages.push(ev.data);
  };
  s.onclose = (ev) => {
    seen.events.push(`close:${ev.code}`);
    seen.closes.push(ev);
  };
  s.onerror = () => seen.events.push('error');
  return seen;
}

beforeEach(() => vi.useRealTimers());
afterEach(() => vi.useRealTimers());

describe('iroh room URL', () => {
  it('round-trips the endpoint and the relay', () => {
    expect(parseIrohUrl(irohUrl(EP, 'http://127.0.0.1:3340'))).toEqual({ endpoint: EP, relay: 'http://127.0.0.1:3340' });
    expect(parseIrohUrl('wss://host/live/x')).toBeNull();
    expect(() => new IrohSocket('wss://host/live/x', { loader: fakeWasm().loader })).toThrow(SyntaxError);
  });
});

describe('IrohSocket', () => {
  it('initialises the module once and connects with the guest ALPN', async () => {
    const w = fakeWasm();
    const a = new IrohSocket(URL_, { loader: w.loader });
    const b = new IrohSocket(URL_, { loader: w.loader });
    await settle();
    expect(w.inits).toBe(1);
    expect(w.calls).toEqual([
      [EP, 'https://relay.example.com', GUEST_ALPN],
      [EP, 'https://relay.example.com', GUEST_ALPN]
    ]);
    expect(a.readyState).toBe(1);
    expect(b.readyState).toBe(1);
    await expect(guestWasm(w.loader)).resolves.toBeDefined();
    expect(w.inits).toBe(1);
  });

  it('opens, then carries text and binary both ways', async () => {
    const w = fakeWasm();
    const s = new IrohSocket(URL_, { loader: w.loader });
    const seen = watch(s);
    expect(s.readyState).toBe(0);
    expect(() => s.send('early')).toThrow();
    await settle();
    expect(seen.events).toEqual(['open']);
    const conn = w.conns[0];

    s.send(JSON.stringify({ type: 'join', token: 't', since: 3 }));
    s.send(new Uint8Array([1, 0, 0, 0, 0, 0, 0, 0, 7, 9]));
    const big = new Uint8Array([5, 6, 7, 8, 9]);
    s.send(big.subarray(1, 3));
    expect(conn.texts).toEqual(['{"type":"join","token":"t","since":3}']);
    expect(conn.sent.map((f) => f.tag)).toEqual([FRAME_TEXT, FRAME_BINARY, FRAME_BINARY]);
    expect([...conn.sent[1].data]).toEqual([1, 0, 0, 0, 0, 0, 0, 0, 7, 9]);
    expect([...conn.sent[2].data]).toEqual([6, 7]);

    conn.text('{"type":"welcome","seq":4}');
    const payload = new Uint8Array([0, 3, 0xaa, 0xbb, 0]).subarray(1, 4); // a view into a bigger buffer
    conn.bin(payload);
    await settle();
    expect(seen.messages[0]).toBe('{"type":"welcome","seq":4}');
    const b = seen.messages[1];
    expect(b).toBeInstanceOf(ArrayBuffer);
    expect([...new Uint8Array(b as ArrayBuffer)]).toEqual([3, 0xaa, 0xbb]);
  });

  it("reports the peer's close code (4410 gone) and frees the connection", async () => {
    const w = fakeWasm();
    const s = new IrohSocket(URL_, { loader: w.loader });
    const seen = watch(s);
    await settle();
    w.conns[0].peerClose(4410, 'gone');
    await settle();
    expect(seen.closes).toEqual([{ code: 4410, reason: 'gone' }]);
    expect(s.readyState).toBe(3);
    expect(w.conns[0].freed).toBe(true);
    s.send('after'); // dropped, as on a closed WebSocket
    expect(w.conns[0].sent).toEqual([]);
  });

  it('a connect that fails is an error, then close 1006', async () => {
    const w = fakeWasm();
    w.outcome = 'reject';
    const s = new IrohSocket(URL_, { loader: w.loader });
    const seen = watch(s);
    await settle();
    expect(seen.events).toEqual(['error', 'close:1006']);
    expect(seen.closes[0].reason).toContain('no route');
    expect(s.readyState).toBe(3);
  });

  it('a module that does not load is an error, then close 1006, and is tried again next time', async () => {
    let tries = 0;
    const loader: GuestWasmLoader = () => {
      tries++;
      return Promise.reject(new Error('CSP blocked the wasm'));
    };
    const seen = watch(new IrohSocket(URL_, { loader }));
    await settle();
    expect(seen.events).toEqual(['error', 'close:1006']);
    watch(new IrohSocket(URL_, { loader }));
    await settle();
    expect(tries).toBe(2);
  });

  it('close() sends our code; onclose follows with it', async () => {
    const w = fakeWasm();
    const s = new IrohSocket(URL_, { loader: w.loader });
    const seen = watch(s);
    await settle();
    s.close(1000, 'bye');
    expect(s.readyState).toBe(2);
    await settle();
    expect(w.conns[0].closedWith).toEqual({ code: 1000, reason: 'bye' });
    expect(seen.closes).toEqual([{ code: 1000, reason: 'bye' }]);
    expect(w.conns[0].freed).toBe(true);
  });

  it('close() while connecting gives 1006 and undoes the late connection', async () => {
    const w = fakeWasm();
    w.outcome = 'hang';
    const s = new IrohSocket(URL_, { loader: w.loader });
    const seen = watch(s);
    await settle();
    s.close();
    await settle();
    expect(seen.events).toEqual(['close:1006']);
    w.pending[0]();
    await settle();
    expect(w.conns[0].closedWith?.code).toBe(1000);
    expect(w.conns[0].freed).toBe(true);
    expect(seen.events).toEqual(['close:1006']);
  });

  it('a malformed frame closes the connection (1002)', async () => {
    const w = fakeWasm();
    const s = new IrohSocket(URL_, { loader: w.loader });
    const seen = watch(s);
    await settle();
    w.conns[0].push(new Error('frame too large'));
    await settle();
    expect(seen.closes).toEqual([{ code: 1002, reason: 'bad frame' }]);
  });

  it('a stream that ends without a close is closed after the grace period', async () => {
    vi.useFakeTimers();
    const w = fakeWasm();
    const s = new IrohSocket(URL_, { loader: w.loader, streamEndGraceMs: 100 });
    const seen = watch(s);
    await settle();
    w.conns[0].push(undefined as unknown as Error); // recv() resolves undefined: the stream ended
    await settle();
    expect(seen.closes).toEqual([]);
    await vi.advanceTimersByTimeAsync(100);
    expect(seen.closes).toEqual([{ code: 1000, reason: 'stream ended' }]);
  });
});
