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
//   ended room (ended: nothing more can change). 4429 (room full) is retried with the same
//   backoff, in state 'full'. A text `ping` every PING_MS; nothing heard for
//   DEAD_MS and the socket counts as dead.
// - sends while not joined are queued and go out after the next welcome (only the latest
//   presence is kept); `queue: false` drops them instead (GuestDoc's notes frames: it works out
//   after a reconnect what the room lacks and sends exactly that).
//
// The host never refuses /live/<room> with an HTTP status (a browser WebSocket cannot see one): an
// unknown, deleted or expired room is upgraded and closed at once with 4410, a full one with 4429.
// A plain failure before the socket opened (1006, e.g. an older host or the network) is still
// opaque; `probe`, when given, is asked after such a failure.
//
// Transports (phase 10): `connect` takes one URL (the host's WebSocket) or a list of transports
// in order of preference, e.g. iroh to the owner's computer, then the host's WebSocket. Until a
// transport has welcomed us, a failure moves on to the next one at once (an attempt with a
// `budgetMs` that has not been welcomed by then counts as failed, while a later one remains);
// after the last, the usual backoff and a new round from the first that may still answer (a
// 4410/4401 rules a transport out). Once welcomed, that transport is kept: every reconnect goes
// to it, under the usual rules. `since` is kept per transport: each is its own log with its own
// seqs (the owner's room and a host mirror).

export type RoomState = 'connecting' | 'live' | 'reconnecting' | 'ended' | 'gone' | 'full' | 'closed';

export const TAG = { FRAME: 0x01, PRESENCE: 0x02, SNAPSHOT: 0x03 } as const;

/** Close codes of /live/<room>. */
export const ROOM_CLOSE = {
  idle: 1001,
  unauthorized: 4401,
  ended: 4403,
  lagged: 4409,
  gone: 4410,
  tooLarge: 4413,
  full: 4429
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
  /** The transport being tried or used changed (its `kind`). */
  onTransport?(kind: string): void;
  /** Every transport failed in a round and none has ever welcomed us: waiting for the next round. */
  onUnreachable?(): void;
}

/** One way to reach the room. */
export interface RoomTransport {
  /** 'iroh' | 'host': what the status line says. */
  kind: string;
  url: string;
  /** The socket constructor; defaults to the client's. */
  webSocket?: RoomSocketCtor;
  /** Awaited before each attempt (loading the iroh wasm); a rejection fails the attempt. */
  prepare?: () => Promise<unknown>;
  /** Not welcomed this long after `prepare` (ms): try the next transport (only while one remains). */
  budgetMs?: number;
  /** Asked after an attempt failed before it opened, on the last transport of a round. */
  probe?: () => Promise<'gone' | 'full' | null>;
  /** Where this transport's log starts (a reconnect's `since`). */
  since?: number;
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
  private transports: RoomTransport[] = [];
  /** Per transport: the highest seq seen. */
  private sinces: number[] = [0];
  /** The transport being tried or used. */
  private cur = 0;
  /** The transport that welcomed us: kept from then on. */
  private stuck: number | null = null;
  /** Transports that said the room is not there (4410/4401) before ever welcoming us. */
  private dead = new Set<number>();
  /** Bumped by every attempt: a prepare or probe that finishes late is ignored. */
  private attempt = 0;
  private budgetTimer: ReturnType<typeof setTimeout> | null = null;
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

  /** The highest seq seen on the current transport: frames, the snapshot and our acks. */
  get since(): number {
    return this.sinces[this.cur] ?? 0;
  }
  set since(v: number) {
    this.sinces[this.cur] = v;
  }

  /** The kind of the transport being tried or used ('iroh', 'host'); null before connect. */
  get transport(): string | null {
    return this.transports[this.cur]?.kind ?? null;
  }

  /** Is the room joined and writable right now? */
  get live(): boolean {
    return this.state === 'live' && this.joined && this.isOpen();
  }

  /** Join through `target`: the room's WebSocket URL, or transports in order of preference. */
  connect(target: string | RoomTransport[], roomToken: string, since = 0): void {
    if (this.wanted) return;
    const list: RoomTransport[] =
      typeof target === 'string' ? [{ kind: 'host', url: target, probe: this.probe, since }] : target;
    if (!list.length) return;
    this.sinces = list.map((t, i) => Math.max(this.sinces[i] ?? 0, t.since ?? 0));
    this.transports = list;
    this.token = roomToken;
    this.dead.clear();
    this.stuck = null;
    this.cur = 0;
    this.wanted = true;
    this.retries = 0;
    this.h.onTransport?.(list[0].kind);
    this.open();
  }

  /** Drop the socket (if any) and join again now, keeping `since`. */
  reconnect(): void {
    if (!this.transports.length || this.state === 'gone' || this.state === 'closed') return;
    this.wanted = true;
    this.clearTimers();
    this.drop(1000);
    this.retries = 0;
    this.moveTo(this.stuck ?? this.firstLive() ?? this.cur);
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
    const n = ++this.attempt;
    const i = this.cur;
    const t = this.transports[i];
    this.ws = null;
    this.joined = false;
    this.opened = false;
    if (!t.prepare) {
      this.openSocket(t, n);
      return;
    }
    let ready: Promise<unknown>;
    try {
      ready = t.prepare();
    } catch (e) {
      ready = Promise.reject(e);
    }
    const current = () => n === this.attempt && this.wanted && !this.ws;
    ready.then(
      () => {
        if (current()) this.openSocket(t, n);
      },
      () => {
        if (current()) this.closed(1006);
      }
    );
  }

  private openSocket(t: RoomTransport, n: number): void {
    const Socket = t.webSocket ?? this.Socket;
    let ws: RoomSocket;
    try {
      ws = new Socket(t.url);
    } catch {
      queueMicrotask(() => {
        if (n === this.attempt && this.wanted && !this.ws) this.closed(1006);
      });
      return;
    }
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
    if (t.budgetMs && this.stuck === null && this.nextAfter(this.cur) !== null) {
      this.budgetTimer = setTimeout(() => {
        this.budgetTimer = null;
        if (ws !== this.ws || this.joined) return;
        this.drop(1000);
        this.closed(1006);
      }, t.budgetMs);
    }
  }

  private nextAfter(i: number): number | null {
    for (let j = i + 1; j < this.transports.length; j++) if (!this.dead.has(j)) return j;
    return null;
  }

  private firstLive(): number | null {
    return this.nextAfter(-1);
  }

  private moveTo(i: number): void {
    if (i === this.cur) return;
    this.cur = i;
    this.h.onTransport?.(this.transports[i].kind);
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
    this.stuck = this.cur;
    if (this.budgetTimer) clearTimeout(this.budgetTimer);
    this.budgetTimer = null;
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
    const i = this.cur;
    const roaming = this.stuck === null;
    const refused = code === ROOM_CLOSE.gone || code === ROOM_CLOSE.unauthorized;
    if (roaming) {
      // Not welcomed on this transport yet: the next one, now.
      if (refused) this.dead.add(i);
      const next = this.nextAfter(i);
      if (next !== null) {
        this.moveTo(next);
        this.open();
        return;
      }
    }
    if (refused) {
      // An earlier transport that only failed may still answer in the next round.
      if (roaming && this.firstLive() !== null) {
        this.retry(code);
        return;
      }
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
    let full = code === ROOM_CLOSE.full;
    const probe = this.transports[i]?.probe;
    if (!opened && !full && probe) {
      const n = this.attempt;
      let why: 'gone' | 'full' | null = null;
      try {
        why = await probe();
      } catch {
        why = null;
      }
      if (!this.wanted || this.ws || n !== this.attempt) return; // closed or reconnected meanwhile
      if (why === 'gone') {
        if (roaming) {
          this.dead.add(i);
          if (this.firstLive() !== null) {
            this.retry(code);
            return;
          }
        }
        this.wanted = false;
        this.queue = [];
        this.setState('gone');
        return;
      }
      full = why === 'full';
    }
    this.setState(full ? 'full' : this.everJoined ? 'reconnecting' : 'connecting');
    this.retry(code);
  }

  /** Try again after the backoff: the kept transport, else a new round from the first that may answer. */
  private retry(code: number): void {
    if (this.stuck === null) this.h.onUnreachable?.();
    this.moveTo(this.stuck ?? this.firstLive() ?? this.cur);
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
    if (this.budgetTimer) clearTimeout(this.budgetTimer);
    this.reconnectTimer = null;
    this.pingTimer = null;
    this.budgetTimer = null;
  }
}
