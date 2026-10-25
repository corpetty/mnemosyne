// The guest page's connection to its room on the publish host: `<ws|wss>://<host>/live/<room>`
// (publish-host/src/live.rs has the protocol). Pure transport: sealed envelopes in and out, no
// crypto, no document. GuestDoc (doc.ts) is the one user.
//
// - join: text `{"type":"join","token","since"}`; the host answers `welcome`, then the snapshot
//   (`0x03||seq u64 BE||env`) when `since` is behind it, then the logged frames after both
//   (`0x01||seq||env`). `onCaughtUp` fires once that replay is in (the highest seq seen reaches
//   welcome.seq, and the snapshot came if one was due).
// - live: others' frames `0x01||seq||env`, presence `0x02||env`, `count`, `ended`, and `ack` for
//   each of our frames in seq order. `since` is the highest seq seen (frames, snapshot, acks):
//   a reconnect asks only for what came after it.
// - reconnects with backoff (RECONNECT_MS) on any close but 4401/4410 (gone) and 4403 or an
//   ended room (ended: nothing more can change). A text `ping` every PING_MS; nothing heard for
//   DEAD_MS and the socket counts as dead.
// - sends while not joined are queued and go out after the next welcome (only the latest
//   presence is kept); `queue: false` drops them instead (GuestDoc's notes frames: it works out
//   after a reconnect what the room lacks and sends exactly that).
//
// A refusal before the upgrade (404/410 gone, 503 full) is invisible to a browser WebSocket: it
// is a close (1006) without an open. `probe`, when given, is asked after such a failure.

export type RoomState = 'connecting' | 'live' | 'reconnecting' | 'ended' | 'gone' | 'full' | 'closed';

export const TAG = { FRAME: 0x01, PRESENCE: 0x02, SNAPSHOT: 0x03 } as const;

/** Close codes of /live/<room>. */
export const ROOM_CLOSE = {
  idle: 1001,
  unauthorized: 4401,
  ended: 4403,
  lagged: 4409,
  gone: 4410,
  tooLarge: 4413
} as const;

/** Wait before reconnect attempt 1, 2, 3, 4, 5+ (ms). */
export const RECONNECT_MS = [1000, 2000, 5000, 10000, 30000] as const;
export const PING_MS = 25_000;
/** Nothing heard from the host for this long: the socket is dead (ms). */
export const DEAD_MS = 60_000;
/** At most this many envelopes wait for a welcome; the oldest go first. */
export const MAX_QUEUE = 512;

const OPEN = 1; // WebSocket.OPEN, without needing the global (tests inject a fake)

/** The part of WebSocket the client uses. */
export interface RoomSocket {
  readyState: number;
  binaryType: string;
  send(data: string | Uint8Array): void;
  close(code?: number, reason?: string): void;
  onopen: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onclose: ((ev: { code: number }) => void) | null;
  onerror: ((ev: unknown) => void) | null;
}
export type RoomSocketCtor = new (url: string) => RoomSocket;

export interface Welcome {
  seq: number;
  snapshot_seq: number | null;
  ended: boolean;
  ended_at: number | null;
  expires: number;
  present: number;
}

export interface RoomHandlers {
  onWelcome?(w: Welcome): void;
  onSnapshot?(seq: number, env: Uint8Array): void;
  onFrame?(seq: number, env: Uint8Array): void;
  onPresence?(env: Uint8Array): void;
  onCount?(present: number): void;
  /** One of our frames passed the relay. `env` is what was given to sendFrame (null if unknown). */
  onAck?(seq: number, env: Uint8Array | null): void;
  /** The replay after a welcome is complete: what follows is live. */
  onCaughtUp?(): void;
  onEnded?(endedAt: number | null, expires: number | null): void;
  /** `{"type":"error","code"}` from the host (frame_too_large, log_full, bad_frame, ...). */
  onError?(code: string): void;
  onState?(state: RoomState): void;
}

export interface RoomOptions {
  webSocket?: RoomSocketCtor;
  /** Asked after a connection failed before it opened: can the page tell why? */
  probe?: () => Promise<'gone' | 'full' | null>;
}

/** The room's socket URL, on the page's own origin. */
export function roomUrl(room: string, loc: { protocol: string; host: string } = location): string {
  return `${loc.protocol === 'https:' ? 'wss' : 'ws'}://${loc.host}/live/${room}`;
}

function readSeq(b: Uint8Array): number {
  return Number(new DataView(b.buffer, b.byteOffset + 1, 8).getBigUint64(0, false));
}

function tagged(tag: number, env: Uint8Array): Uint8Array {
  const out = new Uint8Array(1 + env.length);
  out[0] = tag;
  out.set(env, 1);
  return out;
}

interface Pending {
  tag: number;
  env: Uint8Array;
}

export class RoomClient {
  state: RoomState = 'connecting';
  /** The highest seq seen: frames, the snapshot and our acks. */
  since = 0;
  present = 0;
  ended = false;
  endedAt: number | null = null;
  expires: number | null = null;
  /** Bumped on every welcome: an envelope prepared under another generation is stale. */
  generation = 0;
  /** True between a welcome's replay end and the socket's close. */
  caughtUp = false;
  lastError: string | null = null;

  private readonly Socket: RoomSocketCtor;
  private readonly probe: RoomOptions['probe'];
  private url = '';
  private token = '';
  private ws: RoomSocket | null = null;
  private joined = false;
  private opened = false;
  private wanted = false;
  private everJoined = false;
  private retries = 0;
  private keepBackoff = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private lastHeard = 0;
  private queue: Pending[] = [];
  /** Our frames sent on this socket and not acknowledged yet, in send order. */
  private outstanding: Uint8Array[] = [];
  private catchUp: { target: number; needSnapshot: boolean } | null = null;

  constructor(
    private readonly h: RoomHandlers = {},
    opts: RoomOptions = {}
  ) {
    this.Socket = opts.webSocket ?? (WebSocket as unknown as RoomSocketCtor);
    this.probe = opts.probe;
  }

  /** Is the room joined and writable right now? */
  get live(): boolean {
    return this.state === 'live' && this.joined && this.isOpen();
  }

  connect(url: string, roomToken: string, since = 0): void {
    if (this.wanted) return;
    this.url = url;
    this.token = roomToken;
    this.since = Math.max(this.since, since);
    this.wanted = true;
    this.retries = 0;
    this.open();
  }

  /** Drop the socket (if any) and join again now, keeping `since`. */
  reconnect(): void {
    if (!this.url || this.state === 'gone' || this.state === 'closed') return;
    this.wanted = true;
    this.clearTimers();
    this.drop(1000);
    this.retries = 0;
    this.open();
  }

  /** Stop for good. */
  close(): void {
    this.wanted = false;
    this.clearTimers();
    this.drop(1000);
    this.queue = [];
    this.setState('closed');
  }

  /** A sealed envelope for the log (`0x01`). True when it went out now. */
  sendFrame(env: Uint8Array, opts: { queue?: boolean } = {}): boolean {
    return this.send(TAG.FRAME, env, opts.queue ?? true);
  }

  /** A sealed presence envelope (`0x02`, relayed, never logged). True when it went out now. */
  sendPresence(env: Uint8Array, opts: { queue?: boolean } = {}): boolean {
    return this.send(TAG.PRESENCE, env, opts.queue ?? true);
  }

  // ---- internals -------------------------------------------------------------------------------

  private send(tag: number, env: Uint8Array, queue: boolean): boolean {
    if (this.ended || this.state === 'gone' || this.state === 'closed') return false;
    if (this.joined && this.isOpen()) {
      if (this.write(tag, env)) return true;
    }
    if (!queue) return false;
    if (tag === TAG.PRESENCE) this.queue = this.queue.filter((p) => p.tag !== TAG.PRESENCE);
    this.queue.push({ tag, env });
    if (this.queue.length > MAX_QUEUE) this.queue.splice(0, this.queue.length - MAX_QUEUE);
    return false;
  }

  private write(tag: number, env: Uint8Array): boolean {
    try {
      this.ws?.send(tagged(tag, env));
    } catch {
      return false;
    }
    if (tag === TAG.FRAME) this.outstanding.push(env);
    return true;
  }

  private isOpen(): boolean {
    return this.ws !== null && this.ws.readyState === OPEN;
  }

  private setState(s: RoomState): void {
    if (this.state === s) return;
    this.state = s;
    this.h.onState?.(s);
  }

  private open(): void {
    this.setState(this.everJoined ? 'reconnecting' : 'connecting');
    const ws = new this.Socket(this.url);
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    this.joined = false;
    this.opened = false;
    this.catchUp = null;
    this.caughtUp = false;
    this.outstanding = [];
    ws.onopen = () => {
      if (ws !== this.ws) return;
      this.opened = true;
      this.lastHeard = Date.now();
      try {
        ws.send(JSON.stringify({ type: 'join', token: this.token, since: this.since }));
      } catch {
        /* the close follows */
      }
      this.pingTimer = setInterval(() => this.heartbeat(), PING_MS);
    };
    ws.onmessage = (ev) => {
      if (ws !== this.ws) return;
      this.lastHeard = Date.now();
      this.onData(ev.data);
    };
    ws.onclose = (ev) => {
      if (ws !== this.ws) return;
      this.ws = null;
      this.closed(ev.code);
    };
    ws.onerror = () => {}; // a close follows
  }

  private detach(ws: RoomSocket): void {
    ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
  }

  /** Close the current socket without its close event reaching us. */
  private drop(code: number): void {
    const ws = this.ws;
    this.ws = null;
    this.joined = false;
    this.caughtUp = false;
    this.catchUp = null;
    this.outstanding = [];
    if (!ws) return;
    this.detach(ws);
    try {
      ws.close(code);
    } catch {
      /* already closed */
    }
  }

  private heartbeat(): void {
    if (Date.now() - this.lastHeard >= DEAD_MS) {
      this.drop(1000);
      this.closed(1006);
      return;
    }
    try {
      if (this.isOpen()) this.ws?.send('ping');
    } catch {
      /* the close follows */
    }
  }

  private onData(data: unknown): void {
    if (typeof data === 'string') {
      if (data === 'pong') return;
      let msg: Record<string, unknown>;
      try {
        msg = JSON.parse(data);
      } catch {
        return;
      }
      if (msg && typeof msg === 'object') this.onJson(msg);
    } else if (data instanceof ArrayBuffer) this.onBinary(new Uint8Array(data));
    else if (ArrayBuffer.isView(data)) this.onBinary(new Uint8Array(data.buffer, data.byteOffset, data.byteLength));
  }

  private onJson(msg: Record<string, unknown>): void {
    const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
    switch (msg.type) {
      case 'welcome': {
        const w: Welcome = {
          seq: num(msg.seq) ?? 0,
          snapshot_seq: num(msg.snapshot_seq),
          ended: msg.ended === true,
          ended_at: num(msg.ended_at),
          expires: num(msg.expires) ?? 0,
          present: num(msg.present) ?? 0
        };
        this.welcome(w);
        break;
      }
      case 'count':
        this.present = num(msg.present) ?? this.present;
        this.h.onCount?.(this.present);
        break;
      case 'ack': {
        const seq = num(msg.seq);
        if (seq === null) break;
        this.since = Math.max(this.since, seq);
        this.h.onAck?.(seq, this.outstanding.shift() ?? null);
        break;
      }
      case 'ended':
        this.end(num(msg.ended_at), num(msg.expires));
        break;
      case 'error': {
        const code = typeof msg.code === 'string' ? msg.code : 'error';
        this.lastError = code;
        if (code === 'frame_too_large' || code === 'log_full') this.keepBackoff = true;
        this.h.onError?.(code);
        break;
      }
    }
  }

  private welcome(w: Welcome): void {
    const asked = this.since;
    this.joined = true;
    this.everJoined = true;
    this.generation++;
    if (!this.keepBackoff) this.retries = 0;
    this.keepBackoff = false;
    this.present = w.present;
    this.expires = w.expires || null;
    if (w.ended) {
      this.ended = true;
      this.endedAt = w.ended_at;
      this.queue = [];
    }
    // As the host decides (rooms.rs replay_locked).
    const needSnapshot = w.snapshot_seq !== null && (asked === 0 || asked < w.snapshot_seq);
    this.catchUp = { target: w.seq, needSnapshot };
    this.setState(this.ended ? 'ended' : 'live');
    this.h.onWelcome?.(w);
    if (this.ended) this.h.onEnded?.(this.endedAt, this.expires);
    else {
      const queued = this.queue;
      this.queue = [];
      for (const p of queued) if (!this.write(p.tag, p.env)) this.queue.push(p);
    }
    this.checkCaughtUp();
  }

  private checkCaughtUp(): void {
    const c = this.catchUp;
    if (!c || c.needSnapshot || this.since < c.target) return;
    this.catchUp = null;
    this.caughtUp = true;
    this.h.onCaughtUp?.();
  }

  private onBinary(b: Uint8Array): void {
    if (!this.joined || b.length === 0) return;
    const tag = b[0];
    if (tag === TAG.PRESENCE) {
      this.h.onPresence?.(b.slice(1));
      return;
    }
    if ((tag !== TAG.FRAME && tag !== TAG.SNAPSHOT) || b.length < 9) return;
    const seq = readSeq(b);
    const env = b.slice(9);
    this.since = Math.max(this.since, seq);
    if (tag === TAG.SNAPSHOT) {
      if (this.catchUp) this.catchUp.needSnapshot = false;
      this.h.onSnapshot?.(seq, env);
    } else this.h.onFrame?.(seq, env);
    this.checkCaughtUp();
  }

  private end(endedAt: number | null, expires: number | null): void {
    const was = this.ended;
    this.ended = true;
    this.endedAt = endedAt ?? this.endedAt;
    this.expires = expires ?? this.expires;
    this.queue = [];
    this.setState('ended');
    if (!was || endedAt !== null) this.h.onEnded?.(this.endedAt, this.expires);
  }

  private async closed(code: number): Promise<void> {
    const opened = this.opened;
    this.clearTimers();
    this.joined = false;
    this.caughtUp = false;
    this.catchUp = null;
    this.outstanding = [];
    if (!this.wanted) return;
    if (code === ROOM_CLOSE.gone || code === ROOM_CLOSE.unauthorized) {
      this.wanted = false;
      this.queue = [];
      this.setState('gone');
      return;
    }
    if (code === ROOM_CLOSE.ended) {
      this.wanted = false;
      this.end(null, null);
      return;
    }
    if (this.ended) {
      // Nothing can change in an ended room: keep what we have.
      this.wanted = false;
      return;
    }
    if (code === ROOM_CLOSE.tooLarge) this.keepBackoff = true;
    let full = false;
    if (!opened && this.probe) {
      let why: 'gone' | 'full' | null = null;
      try {
        why = await this.probe();
      } catch {
        why = null;
      }
      if (!this.wanted || this.ws) return; // closed or reconnected meanwhile
      if (why === 'gone') {
        this.wanted = false;
        this.queue = [];
        this.setState('gone');
        return;
      }
      full = why === 'full';
    }
    this.setState(full ? 'full' : this.everJoined ? 'reconnecting' : 'connecting');
    // A lagged client rejoins at once: it was only too slow, not refused.
    const delay = code === ROOM_CLOSE.lagged ? 0 : RECONNECT_MS[Math.min(this.retries, RECONNECT_MS.length - 1)];
    if (code !== ROOM_CLOSE.lagged) this.retries++;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (this.wanted && !this.ws) this.open();
    }, delay);
  }

  private clearTimers(): void {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    if (this.pingTimer) clearInterval(this.pingTimer);
    this.reconnectTimer = null;
    this.pingTimer = null;
  }
}
