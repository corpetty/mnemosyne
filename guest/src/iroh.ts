// A room socket over iroh (phase 10): the WebSocket-like `RoomSocket` that RoomClient (room.ts)
// uses, on top of the browser iroh client (`guest/pkg/guest_wasm.js`, guest-wasm/). The owner's
// sidecar proxies one bi stream per guest to the room's phase-9 WebSocket, so what travels is the
// same: text frames (tag 0, UTF-8 JSON) and binary frames (tag 1), then the room's close code as
// the QUIC application close (4410 "gone", ...; 1006 on transport errors).
//
// The wasm module is loaded on first use and initialised once, through a loader: the page's is
// guest-wasm.ts (a dynamic import, so the first load does not carry it); tests give a fake. This
// module never imports the wasm itself, so tests run without guest/pkg/.
import type { RoomSocket } from './room';

/** The ALPN of a room's guest endpoint (link/: never the remote-access one). */
export const GUEST_ALPN = 'mnemosyne/guest/1';

export const FRAME_TEXT = 0;
export const FRAME_BINARY = 1;

/** What guest_wasm's `GuestConn` offers (its generated types say `any` for the promises). */
export interface GuestConn {
  send(tag: number, bytes: Uint8Array): Promise<void>;
  /** One pending at a time; undefined once the stream or the connection ended. */
  recv(): Promise<{ tag: number; data: Uint8Array } | undefined>;
  close(code: number, reason: string): Promise<void>;
  /** The peer's application close, ours, or 1006 when the transport failed. */
  closed(): Promise<{ code: number; reason: string }>;
  free(): void;
}

export interface GuestWasmModule {
  /** wasm-bindgen's `init()`: fetches and instantiates the .wasm beside the module. */
  default(moduleOrPath?: unknown): Promise<unknown>;
  connect(endpointIdHex: string, relayUrl: string, alpn: string): Promise<GuestConn>;
}

export type GuestWasmLoader = () => Promise<GuestWasmModule>;

const ready = new WeakMap<GuestWasmLoader, Promise<GuestWasmModule>>();

/** The module, imported and initialised once per loader (a failure is retried next time). */
export function guestWasm(loader: GuestWasmLoader): Promise<GuestWasmModule> {
  let p = ready.get(loader);
  if (!p) {
    p = loader().then(async (m) => {
      await m.default();
      return m;
    });
    const mine = p;
    p.catch(() => {
      if (ready.get(loader) === mine) ready.delete(loader);
    });
    ready.set(loader, p);
  }
  return p;
}

/** The socket "URL" RoomClient is given for an iroh room: `iroh:<endpoint id>?relay=<relay url>`. */
export function irohUrl(endpoint: string, relay: string): string {
  return `iroh:${endpoint}?relay=${encodeURIComponent(relay)}`;
}

export function parseIrohUrl(url: string): { endpoint: string; relay: string } | null {
  const m = /^iroh:([0-9A-Za-z]+)\?relay=([^&]+)$/.exec(url);
  if (!m) return null;
  try {
    return { endpoint: m[1], relay: decodeURIComponent(m[2]) };
  } catch {
    return null;
  }
}

export interface IrohSocketOptions {
  /** Where the wasm module comes from (guest-wasm.ts `loadGuestWasm` on the page). */
  loader: GuestWasmLoader;
  alpn?: string;
  /** After the stream ended without the connection closing, close it ourselves after this (ms). */
  streamEndGraceMs?: number;
}

const CONNECTING = 0;
const OPEN = 1;
const CLOSING = 2;
const CLOSED = 3;

const encoder = new TextEncoder();
const decoder = new TextDecoder();

export class IrohSocket implements RoomSocket {
  readyState = CONNECTING;
  /** Kept for the interface: binary messages always arrive as ArrayBuffer. */
  binaryType = 'arraybuffer';
  onopen: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: ((ev: { code: number; reason?: string }) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;

  readonly url: string;
  private conn: GuestConn | null = null;
  private recvDone = false;
  private closedDone = false;
  private freed = false;
  private readonly grace: number;

  constructor(url: string, opts: IrohSocketOptions) {
    const target = parseIrohUrl(url);
    if (!target) throw new SyntaxError(`not an iroh room address: ${url}`);
    this.url = url;
    this.grace = opts.streamEndGraceMs ?? 3000;
    void this.start(target.endpoint, target.relay, opts.loader, opts.alpn ?? GUEST_ALPN);
  }

  send(data: string | ArrayBufferView | ArrayBuffer): void {
    if (this.readyState === CONNECTING) throw new DOMException('Still connecting', 'InvalidStateError');
    if (this.readyState !== OPEN || !this.conn) return; // as a WebSocket: dropped once closing
    let tag: number;
    let bytes: Uint8Array;
    if (typeof data === 'string') {
      tag = FRAME_TEXT;
      bytes = encoder.encode(data);
    } else if (data instanceof ArrayBuffer) {
      tag = FRAME_BINARY;
      bytes = new Uint8Array(data);
    } else if (ArrayBuffer.isView(data)) {
      tag = FRAME_BINARY;
      bytes = new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
    } else throw new TypeError('send() takes a string or bytes');
    // Frames go out in call order. One that cannot be written means the stream lost it: close,
    // so the room client rejoins and the room's replay says what it has.
    this.conn.send(tag, bytes).catch(() => {
      if (this.readyState === OPEN) this.close(1011, 'send failed');
    });
  }

  close(code = 1000, reason = ''): void {
    if (this.readyState === CLOSING || this.readyState === CLOSED) return;
    if (this.readyState === CONNECTING || !this.conn) {
      // As a WebSocket closed before it opened: 1006. The connect still running is undone in start().
      this.readyState = CLOSING;
      queueMicrotask(() => this.finish(1006, 'closed before the connection was established'));
      return;
    }
    this.readyState = CLOSING;
    // closed() then resolves with this code (our own close), which reaches onclose.
    this.conn.close(code, reason).catch(() => {});
  }

  // ---- internals -------------------------------------------------------------------------------

  private async start(endpoint: string, relay: string, loader: GuestWasmLoader, alpn: string): Promise<void> {
    let conn: GuestConn;
    try {
      const wasm = await guestWasm(loader);
      if (this.readyState !== CONNECTING) return;
      conn = await wasm.connect(endpoint, relay, alpn);
    } catch (e) {
      if (this.readyState !== CONNECTING) return;
      this.readyState = CLOSING;
      this.onerror?.({ type: 'error', error: e });
      this.finish(1006, e instanceof Error ? e.message : String(e));
      return;
    }
    if (this.readyState !== CONNECTING) {
      // Closed while connecting: nobody wants this connection.
      conn
        .close(1000, '')
        .catch(() => {})
        .finally(() => conn.free());
      return;
    }
    this.conn = conn;
    this.readyState = OPEN;
    conn.closed().then(
      (info) => this.peerClosed(info?.code ?? 1006, info?.reason ?? ''),
      (e) => this.peerClosed(1006, e instanceof Error ? e.message : String(e))
    );
    this.onopen?.({ type: 'open' });
    void this.recvLoop(conn);
  }

  private async recvLoop(conn: GuestConn): Promise<void> {
    let ended = false;
    for (;;) {
      let f: { tag: number; data: Uint8Array } | undefined;
      try {
        f = await conn.recv();
      } catch {
        // A malformed frame (or the connection failing under the read).
        if (this.readyState === OPEN) this.close(1002, 'bad frame');
        break;
      }
      if (!f) {
        ended = true;
        break;
      }
      if (this.readyState !== OPEN) continue;
      this.deliver(f.tag, f.data);
    }
    this.recvDone = true;
    if (ended && this.readyState === OPEN) {
      // The stream finished; the connection's close normally follows at once.
      setTimeout(() => {
        if (this.readyState === OPEN) this.close(1000, 'stream ended');
      }, this.grace);
    }
    this.maybeFree();
  }

  private deliver(tag: number, data: Uint8Array): void {
    if (tag === FRAME_TEXT) this.onmessage?.({ data: decoder.decode(data) });
    else if (tag === FRAME_BINARY)
      this.onmessage?.({ data: data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength) });
  }

  private peerClosed(code: number, reason: string): void {
    this.closedDone = true;
    this.finish(code, reason);
    this.maybeFree();
  }

  /** The one close event. */
  private finish(code: number, reason: string): void {
    if (this.readyState === CLOSED) return;
    this.readyState = CLOSED;
    this.onclose?.({ code, reason });
  }

  private maybeFree(): void {
    if (this.freed || !this.conn || !this.closedDone || !this.recvDone) return;
    this.freed = true;
    try {
      this.conn.free();
    } catch {
      /* already gone */
    }
  }
}

/** A RoomSocket constructor for RoomClient with these options bound. */
export function irohSocketCtor(opts: IrohSocketOptions): new (url: string) => IrohSocket {
  return class extends IrohSocket {
    constructor(url: string) {
      super(url, opts);
    }
  };
}
