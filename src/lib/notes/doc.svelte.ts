// A meeting's live notes document, connected to the relay at /ws/notes/{session_id}
// (docs/api-reference.md "Live notes"). One instance per open meeting; the component that
// shows the notes creates it, calls `connect`, and `destroy`s it. Not a singleton.
//
// The hashseq store (`WasmHashWeb`) lives as long as the instance, across socket reconnects, so
// edits typed while the socket was down are never lost. What the store sends is always "what
// the relay lacks": `takeDeltas` diffs against the *upstream clock*, and the clock is set from
// the relay's own frontier on every (re)connect:
//
//   1. the hello gives snapshot + backlog; a throwaway replica of those (`openFromHello`) yields
//      F = its frontier, exactly what the relay holds;
//   2. snapshot and backlog are merged into the live store (no sending: the upstream clock is
//      about to be replaced);
//   3. `applyFrontier(F)`: the next drain is the ops the relay lacks, whether they were lost in
//      the drop, typed while offline, or never drained;
//   4. every artifact frame this client ever minted is sent again first (`takeNewArtifacts`
//      only returns new ones; they are tiny and a duplicate is harmless).
//
// While the socket is not live nothing is drained or queued: ops simply stay in the store, and
// step 3 picks them up. Nothing here authors an op on open (no `ensureTerminated`): the editor
// copes with an empty document.
import { openDoc, type Doc } from './model.js';
import { ARTIFACT_TAG, DELTA_TAG, applyRemote, drain, openFromHello } from './sync.js';
import type { WasmHashWeb } from './wasm.js';
import { connectionState } from '../stores/connection.svelte.js';
import type { NotesClientMessage, NotesHello, NotesPerson, NotesServerMessage } from '../types/index.js';

export type NotesStatus =
  | 'connecting' // first connect, no document yet
  | 'catching_up' // hello received, snapshot / backlog arriving
  | 'live'
  | 'reconnecting' // dropped: the document is kept and editable locally
  | 'unavailable' // the backend has no sidecar: use the plain-text editor
  | 'gone' // 4404: the meeting is gone or hidden from us
  | 'error'; // refused or broken (see `error`); not retried

/** Close codes of /ws/notes/{id}. */
export const NOTES_CLOSE = {
  gone: 4404,
  readOnly: 4403,
  badFrame: 4400,
  tooBig: 1009
} as const;

/** Wait before reconnect attempt 1, 2, 3, 4+ (ms). */
export const RECONNECT_MS = [1000, 2000, 5000, 10000] as const;
/** A drain runs this long after the last local change (ms), at most MAX_WAIT_MS after the first. */
export const DEBOUNCE_MS = 200;
export const MAX_WAIT_MS = 1000;
/** `typing: false` is sent after this much idle time (ms). */
export const TYPING_IDLE_MS = 2000;
export const PING_MS = 25_000;
/** Nothing heard from the relay for this long: the socket is dead (ms). */
export const DEAD_MS = 60_000;

const OPEN = 1; // WebSocket.OPEN, without needing the global (tests inject a fake)

/** The part of WebSocket the store uses. */
export interface NotesSocket {
  readyState: number;
  binaryType: string;
  send(data: string | Uint8Array): void;
  close(code?: number, reason?: string): void;
  onopen: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onclose: ((ev: { code: number }) => void) | null;
  onerror: ((ev: unknown) => void) | null;
}
export type NotesSocketCtor = new (url: string) => NotesSocket;

export interface NotesDocOptions {
  /** The WebSocket constructor (tests inject a fake). */
  webSocket?: NotesSocketCtor;
  /** The socket URL for a meeting; defaults to the connection store's backend and token. */
  url?: (sessionId: string) => string;
  /** Called after the document changed because of the relay (and `version` was bumped). */
  onRemoteChange?: () => void;
  /**
   * The editor's `input.applyRemote`: every change the relay makes to an existing document runs
   * inside it, so the editor can capture its selection by element id BEFORE the text changes and
   * re-render after (offsets read from the DOM are only valid against the text it shows). It
   * must call `apply` exactly once, synchronously.
   */
  wrapRemote?: (apply: () => void) => void;
}

interface CatchUp {
  hello: NotesHello;
  needSnapshot: boolean;
  snapshot: Uint8Array | null;
  frames: Uint8Array[];
}

function defaultUrl(sessionId: string): string {
  const base = connectionState.url.replace(/^http/, 'ws') + '/ws/notes/' + encodeURIComponent(sessionId);
  return connectionState.token ? `${base}?token=${encodeURIComponent(connectionState.token)}` : base;
}

/** Apply one frame without sending anything (the upstream clock is replaced right after). */
function applyQuiet(web: WasmHashWeb, frame: Uint8Array): void {
  if (frame.length === 0) return;
  if (frame[0] === ARTIFACT_TAG) web.provideArtifactBytes(frame.subarray(1));
  else if (frame[0] === DELTA_TAG) web.applyDelta(frame);
  else throw new Error(`unexpected notes frame tag 0x${frame[0].toString(16)}`);
}

export class NotesDocState {
  status = $state<NotesStatus>('connecting');
  /** Why `status` is 'error'. */
  error = $state<string | null>(null);
  readOnly = $state(false);
  /** A meeting recorded elsewhere whose document has not come by library sync yet (read-only). */
  waitingForSync = $state(false);
  present = $state<string[]>([]);
  typing = $state<string[]>([]);
  /** Guests on the published notes link: who is in the room and who is typing. */
  guests = $state<string[]>([]);
  guestsTyping = $state<string[]>([]);
  lastEdit = $state<{ by: string; at: string } | null>(null);
  you = $state.raw<NotesPerson | null>(null);
  /** The document; null until the first hello, and for ever when unavailable. */
  doc = $state.raw<Doc | null>(null);
  /** Bumped on every change that came from the relay: the editor re-renders on it. */
  version = $state(0);

  onRemoteChange: (() => void) | undefined;
  wrapRemote: ((apply: () => void) => void) | undefined;

  private readonly Socket: NotesSocketCtor;
  private readonly urlFor: (sessionId: string) => string;
  private sessionId = '';
  private web: WasmHashWeb | null = null;
  private ws: NotesSocket | null = null;
  private wanted = false;
  private destroyed = false;
  private retries = 0;
  private catchUp: CatchUp | null = null;
  /** Every artifact frame this client minted, re-sent after each reconnect. */
  private artifacts: Uint8Array[] = [];
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private drainTimer: ReturnType<typeof setTimeout> | null = null;
  private dirtySince: number | null = null;
  private typingTimer: ReturnType<typeof setTimeout> | null = null;
  private typingSent = false;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private lastHeard = 0;
  private pongWaiters: (() => void)[] = [];

  constructor(opts: NotesDocOptions = {}) {
    this.Socket = opts.webSocket ?? (WebSocket as unknown as NotesSocketCtor);
    this.urlFor = opts.url ?? defaultUrl;
    this.onRemoteChange = opts.onRemoteChange;
    this.wrapRemote = opts.wrapRemote;
  }

  /** Run a change the relay makes to the document inside the editor's wrapper, if it has one. */
  private remote<T>(fn: () => T): T {
    let out: T | undefined;
    const run = () => {
      out = fn();
    };
    if (this.wrapRemote) this.wrapRemote(run);
    else run();
    return out as T;
  }

  // ---- lifecycle ---------------------------------------------------------------------------

  connect(sessionId: string): void {
    if (this.wanted) return;
    this.sessionId = sessionId;
    this.destroyed = false;
    this.wanted = true;
    this.retries = 0;
    this.error = null;
    this.status = this.web ? 'reconnecting' : 'connecting';
    this.open();
  }

  /**
   * Rejoin now: drop the socket (if any) and open a new one, keeping the document, so what we
   * typed meanwhile is sent after the catch-up. For the relay's `reload` (the stored document
   * was replaced by library sync), a meeting that was waiting for its notes, and the error
   * state's "Reconnect" button. A no-op once destroyed.
   */
  reconnect(): void {
    if (!this.sessionId || this.destroyed) return;
    if (!this.wanted) {
      // After an error / 4404 / unavailable: a user-requested retry starts again.
      if (this.status === 'unavailable') return;
      this.wanted = true;
    }
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    if (this.pingTimer) clearInterval(this.pingTimer);
    this.pingTimer = null;
    const ws = this.ws;
    this.ws = null;
    this.catchUp = null;
    if (ws) {
      this.detach(ws);
      try {
        ws.close(1000);
      } catch {
        /* already closed */
      }
    }
    this.retries = 0;
    this.error = null;
    this.typing = [];
    this.typingSent = false;
    this.status = this.web ? 'reconnecting' : 'connecting';
    this.open();
  }

  /** Stop for good: close the socket, cancel every timer. The document stays readable. */
  destroy(): void {
    this.destroyed = true;
    this.wanted = false;
    this.clearTimers();
    const ws = this.ws;
    this.ws = null;
    this.catchUp = null;
    if (ws) {
      this.detach(ws);
      try {
        ws.close(1000);
      } catch {
        /* already closed */
      }
    }
  }

  // ---- calls from the editor -------------------------------------------------------------------

  /**
   * Change the document from outside the editor (the Copilot's "Add to notes"): `fn` runs inside
   * the editor's wrapper, so the view keeps its selection and re-renders, then the change is sent
   * like a typed one. Does nothing (returns undefined) until the document exists.
   */
  applyLocal<T>(fn: (doc: Doc) => T): T | undefined {
    const doc = this.doc;
    if (!doc) return undefined;
    const out = this.remote(() => fn(doc));
    this.localChange();
    this.changed();
    return out;
  }

  /** The editor ran a command: send what it produced soon (200 ms debounce, 1 s at most). */
  localChange(): void {
    if (!this.web || this.readOnly || this.status !== 'live') return; // a reconnect drains it
    const now = Date.now();
    if (this.dirtySince === null) this.dirtySince = now;
    if (this.drainTimer) clearTimeout(this.drainTimer);
    const wait = Math.min(DEBOUNCE_MS, Math.max(0, MAX_WAIT_MS - (now - this.dirtySince)));
    this.drainTimer = setTimeout(() => this.flush(), wait);
  }

  /** Drain and send now (the editor may call it on blur or before leaving). */
  flush(): void {
    if (this.drainTimer) clearTimeout(this.drainTimer);
    this.drainTimer = null;
    this.dirtySince = null;
    const web = this.web;
    if (!web || this.readOnly || this.status !== 'live' || !this.isOpen()) return;
    this.sendFrames(this.track(drain(web)));
  }

  /**
   * Send what is pending and resolve once the relay has handled it (a ping answered after the
   * frames, on the same socket), or after `timeoutMs`, or at once when there is no live socket.
   * For an HTTP request that must see the document as this client has it (a comment on a
   * selection: the server marks the characters, so it must have them).
   */
  synced(timeoutMs = 2000): Promise<void> {
    this.flush();
    if (!this.isOpen() || this.status !== 'live') return Promise.resolve();
    return new Promise((resolve) => {
      const done = () => {
        clearTimeout(timer);
        resolve();
      };
      const timer = setTimeout(() => {
        const i = this.pongWaiters.indexOf(done);
        if (i >= 0) this.pongWaiters.splice(i, 1);
        resolve();
      }, timeoutMs);
      this.pongWaiters.push(done);
      if (!this.sendJson({ type: 'ping' })) done();
    });
  }

  /**
   * The user is (not) typing. `true` tells the relay at once and then only keeps the idle
   * timer fresh; `false` goes out after 2 s without a `true`, or immediately when passed.
   */
  setTyping(on: boolean): void {
    if (this.readOnly || !this.web) return;
    if (this.typingTimer) clearTimeout(this.typingTimer);
    this.typingTimer = null;
    if (on) {
      if (!this.typingSent && this.sendJson({ type: 'presence', typing: true })) this.typingSent = true;
      this.typingTimer = setTimeout(() => {
        this.typingTimer = null;
        this.stopTyping();
      }, TYPING_IDLE_MS);
    } else this.stopTyping();
  }

  // ---- socket --------------------------------------------------------------------------------

  private open(): void {
    const ws = new this.Socket(this.urlFor(this.sessionId));
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    this.catchUp = null;
    // Every handler checks it is still the current socket: a replaced one's late events must
    // not touch the state.
    ws.onopen = () => {
      if (ws !== this.ws) return;
      this.lastHeard = Date.now();
      this.pingTimer = setInterval(() => this.heartbeat(), PING_MS);
    };
    ws.onmessage = (ev) => {
      if (ws !== this.ws) return;
      this.lastHeard = Date.now();
      try {
        this.onData(ev.data);
      } catch (e) {
        this.fail(e instanceof Error ? e.message : String(e));
      }
    };
    ws.onclose = (ev) => {
      if (ws !== this.ws) return;
      this.ws = null;
      this.closed(ev.code);
    };
    ws.onerror = () => {}; // a close follows
  }

  private isOpen(): boolean {
    return this.ws !== null && this.ws.readyState === OPEN;
  }

  private detach(ws: NotesSocket): void {
    ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
  }

  private sendJson(msg: NotesClientMessage): boolean {
    if (!this.isOpen()) return false;
    try {
      this.ws?.send(JSON.stringify(msg));
      return true;
    } catch {
      return false;
    }
  }

  /** Send frames when the socket is live and we may edit; otherwise the next reconnect resyncs. */
  private sendFrames(frames: Uint8Array[]): void {
    if (this.readOnly || this.status !== 'live' || !this.isOpen()) return;
    for (const f of frames) {
      try {
        this.ws?.send(f);
      } catch {
        return;
      }
    }
  }

  /** Remember artifact frames so a reconnect can send them again. */
  private track(frames: Uint8Array[]): Uint8Array[] {
    for (const f of frames) if (f[0] === ARTIFACT_TAG) this.artifacts.push(f);
    return frames;
  }

  private heartbeat(): void {
    if (Date.now() - this.lastHeard >= DEAD_MS) {
      const ws = this.ws;
      this.ws = null;
      if (ws) {
        this.detach(ws);
        try {
          ws.close();
        } catch {
          /* dead anyway */
        }
      }
      this.closed(1006);
      return;
    }
    this.sendJson({ type: 'ping' });
  }

  private stopTyping(): void {
    if (this.typingSent && this.sendJson({ type: 'presence', typing: false })) this.typingSent = false;
  }

  // ---- incoming ------------------------------------------------------------------------------

  private onData(data: unknown): void {
    if (typeof data === 'string') {
      let msg: NotesServerMessage;
      try {
        msg = JSON.parse(data);
      } catch {
        return;
      }
      this.onJson(msg);
    } else if (data instanceof ArrayBuffer) this.onBinary(new Uint8Array(data));
    else if (ArrayBuffer.isView(data)) this.onBinary(new Uint8Array(data.buffer, data.byteOffset, data.byteLength));
  }

  private onJson(msg: NotesServerMessage): void {
    switch (msg.type) {
      case 'hello':
        if ('unavailable' in msg && msg.unavailable) {
          this.wanted = false; // the close that follows is not a drop
          this.clearTimers();
          this.status = 'unavailable';
          const ws = this.ws;
          this.ws = null;
          if (ws) {
            this.detach(ws);
            try {
              ws.close(1000);
            } catch {
              /* already closed */
            }
          }
        } else this.beginCatchUp(msg as NotesHello);
        break;
      case 'presence':
        this.present = msg.present;
        this.typing = msg.typing;
        break;
      case 'guests':
        this.guests = msg.present;
        this.guestsTyping = msg.typing;
        break;
      case 'last_edit':
        this.lastEdit = { by: msg.by, at: msg.at };
        break;
      case 'reload':
        this.reconnect(); // the close that follows belongs to the old socket
        break;
      case 'pong':
        for (const done of this.pongWaiters.splice(0)) done();
        break;
    }
  }

  private beginCatchUp(hello: NotesHello): void {
    const waiting = hello.waiting_for_sync === true;
    this.you = hello.you;
    this.readOnly = hello.read_only || waiting;
    this.waitingForSync = waiting;
    this.present = hello.present;
    this.typing = [];
    this.guests = [];
    this.guestsTyping = [];
    this.lastEdit = hello.last_edit;
    this.status = 'catching_up';
    this.catchUp = { hello, needSnapshot: hello.snapshot, snapshot: null, frames: [] };
    if (this.catchUpDone()) this.finishCatchUp();
  }

  private catchUpDone(): boolean {
    const c = this.catchUp;
    return c !== null && (!c.needSnapshot || c.snapshot !== null) && c.frames.length >= c.hello.backlog;
  }

  private onBinary(frame: Uint8Array): void {
    const c = this.catchUp;
    if (c) {
      if (c.needSnapshot && c.snapshot === null) c.snapshot = frame;
      else c.frames.push(frame);
      if (this.catchUpDone()) this.finishCatchUp();
      return;
    }
    const web = this.web;
    if (!web || this.status !== 'live') return;
    const { send, delivered } = this.remote(() => applyRemote(web, frame));
    this.sendFrames(this.track(send));
    if (delivered > 0) this.changed();
  }

  /** The relay's whole state has arrived: build the document, or sync the one we have. */
  private finishCatchUp(): void {
    const c = this.catchUp as CatchUp;
    this.catchUp = null;
    const snapshot = c.snapshot && c.snapshot.length ? c.snapshot : null;
    if (!this.web) {
      this.web = openFromHello(snapshot, c.frames);
      this.doc = openDoc(this.web, c.hello.notes);
    } else {
      if (this.doc && this.doc.obj !== openDoc(this.web, c.hello.notes).obj) {
        this.fail('the notes document changed identity');
        return;
      }
      const web = this.web;
      // 1. what the relay holds, as a frontier
      const temp = openFromHello(snapshot, c.frames);
      const frontier = temp.encodeFrontier();
      temp.free();
      // 2. merge it into the live store, sending nothing
      this.remote(() => {
        if (snapshot) web.mergeEncoded(snapshot);
        for (const f of c.frames) applyQuiet(web, f);
      });
      // 3. the relay's clock is now exactly F: the next drain is what it lacks
      web.applyFrontier(frontier);
    }
    this.status = 'live';
    this.retries = 0;
    this.typingSent = false;
    if (this.web && !this.readOnly) {
      // 4. artifacts again (duplicates are harmless), then the missing ops
      this.sendFrames([...this.artifacts]);
      this.sendFrames(this.track(drain(this.web)));
    }
    this.changed();
  }

  private changed(): void {
    this.version++;
    this.onRemoteChange?.();
  }

  // ---- closing and retrying ------------------------------------------------------------------------

  private closed(code: number): void {
    this.clearTimers();
    this.catchUp = null;
    this.typing = [];
    this.typingSent = false;
    if (!this.wanted) return;
    if (code === NOTES_CLOSE.gone) {
      this.wanted = false;
      this.status = 'gone';
    } else if (code === NOTES_CLOSE.readOnly) {
      this.wanted = false;
      this.readOnly = true;
      this.error = 'The relay refused an edit from a read-only connection';
      this.status = 'error';
    } else if (code === NOTES_CLOSE.badFrame || code === NOTES_CLOSE.tooBig) {
      // Resyncing would send the same frame again.
      this.wanted = false;
      this.error = code === NOTES_CLOSE.tooBig ? 'A notes update was too big for the relay' : 'The relay refused a notes frame';
      this.status = 'error';
    } else {
      this.status = this.web ? 'reconnecting' : 'connecting';
      const delay = RECONNECT_MS[Math.min(this.retries, RECONNECT_MS.length - 1)];
      this.retries++;
      this.reconnectTimer = setTimeout(() => {
        this.reconnectTimer = null;
        if (this.wanted && !this.ws) this.open();
      }, delay);
    }
  }

  /** Something is wrong with what the relay sent: stop, keep the document. */
  private fail(message: string): void {
    this.wanted = false;
    this.clearTimers();
    this.catchUp = null;
    this.error = message;
    this.status = 'error';
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      this.detach(ws);
      try {
        ws.close(1000);
      } catch {
        /* already closed */
      }
    }
  }

  private clearTimers(): void {
    for (const t of [this.reconnectTimer, this.drainTimer, this.typingTimer]) if (t) clearTimeout(t);
    if (this.pingTimer) clearInterval(this.pingTimer);
    this.reconnectTimer = this.drainTimer = this.typingTimer = null;
    this.pingTimer = null;
    this.dirtySince = null;
  }
}
