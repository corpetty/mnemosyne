// The guest page's notes document (phase 9): a hashseq store kept in step with a room on the
// publish host through RoomClient (room.ts), every frame sealed with the link's key (format.ts).
// The page's counterpart of the app's NotesDocState (src/lib/notes/doc.svelte.ts), without app
// stores or runes: plain fields, `subscribe` for changes, the same editor-facing surface (`doc`,
// `readOnly`, `version`, `wrapRemote`, `onRemoteChange`, `localChange`, `flush`, `setTyping`,
// `synced`, `applyLocal`).
//
// Two stores:
// - `mirror`: exactly what the room holds as far as we have seen, in seq order: the snapshot,
//   every logged notes frame (ours once acknowledged). Never edited, never drained.
// - `web`: the live document the editor works on: the mirror plus our own edits.
// A reconnect asks the room only for what came after `since`; that replay goes into the mirror,
// the mirror is merged into `web` without sending, and `web.applyFrontier(mirror's frontier)`
// makes the next drain exactly what the room lacks: edits typed while offline, frames lost in
// the drop, nothing it already logged (frames sent but not acknowledged come back in the
// replay). Artifact frames we minted that the mirror lacks are sent again first. This is the
// app's reconnect (doc.svelte.ts steps 1-4) with a persistent replica instead of a throwaway
// one, because the room's replay is partial.
//
// While not live nothing is drained: ops stay in `web` and the reconnect picks them up. A drained
// frame whose seal finishes after the socket changed is dropped for the same reason.
//
// Envelopes are opened in arrival order on one promise chain (WebCrypto is async) and sealed in
// drain order on another, so artifacts still go before the delta that needs them.
import { openDoc, type Doc } from '$lib/notes/model.js';
import { ARTIFACT_TAG, DELTA_TAG, applyRemote, drain } from '$lib/notes/sync.js';
import { WasmHashWeb } from '$lib/notes/wasm.js';
import { Kind, b64urlDecode, parseNotes, sealJson, sealNotes, unseal, type GuestKeys } from './format';
import { RoomClient, roomUrl, type RoomSocketCtor, type RoomState, type Welcome } from './room';

export type GuestStatus =
  | 'connecting' // nothing joined yet, no document
  | 'catching_up' // joined, the replay is arriving
  | 'live'
  | 'reconnecting' // dropped: the document is kept and editable locally
  | 'ended' // the room is read-only for good (until it expires)
  | 'gone' // deleted, expired or a wrong link
  | 'full' // the host refused: too many people (only known with a probe)
  | 'error'; // something the room sent could not be used (see `error`)

/** A drain runs this long after the last local change (ms), at most MAX_WAIT_MS after the first. */
export const DEBOUNCE_MS = 60;
export const MAX_WAIT_MS = 1000;
/** `typing: false` is sent after this much idle time (ms). */
export const TYPING_IDLE_MS = 2000;
/** Our presence goes out this often (the bridge drops a guest after 30 s without one). */
export const PRESENCE_MS = 15_000;
/** Someone else's presence counts for this long (ms). */
export const PRESENCE_TTL_MS = 30_000;

const ORIGIN_HEX = /^[0-9a-f]{64}$/;

export interface AgendaItem {
  text: string;
  covered: boolean;
}

export interface GuestMeta {
  title: string;
  date: string;
  owner: string;
  agenda: AgendaItem[];
  ended: boolean;
  ended_at: string;
}

/** A comment as the bridge seals it for guests (guest_notes.py `_guest_comment`). */
export interface GuestComment {
  id: string;
  parent_id: string | null;
  author: string;
  /** A guest's own comments carry their gid; "" for everyone else. */
  gid: string;
  text: string;
  quote: string;
  mark: string;
  created_at: string;
  updated_at: string;
  resolved: boolean;
  deleted: boolean;
  detached: boolean;
  changed: boolean;
}

export interface GuestThread {
  root: GuestComment;
  replies: GuestComment[];
}

export interface Person {
  /** "" is the meeting's owner. */
  gid: string;
  name: string;
  typing: boolean;
  /** When we last heard from them (ms, `now()`). */
  at: number;
}

export interface GuestDocOptions {
  room: string;
  keys: GuestKeys;
  /** This page's random guest id ([0-9A-Za-z_-]{1,64}; never ""). */
  gid: string;
  /** "" on a first visit: the room is read, nothing is announced or said until `setName`. */
  name: string;
  /** The socket URL; defaults to /live/<room> on the page's origin. */
  url?: string;
  webSocket?: RoomSocketCtor;
  probe?: () => Promise<'gone' | 'full' | null>;
  /** The notes object's origin when known up front; else it comes with the snapshot or meta. */
  origin?: string;
  /** Called after anything observable changed (status, meta, threads, people, document). */
  onChange?: () => void;
  /** As NotesDocState's: after the document changed because of the room. */
  onRemoteChange?: () => void;
  /** As NotesDocState's: the editor's `input.applyRemote`. */
  wrapRemote?: (apply: () => void) => void;
  /** A guest's comment op relayed by the room (create/reply/edit/delete; not `threads`). */
  onGuestComment?: (op: Record<string, unknown>) => void;
  now?: () => number;
}

/** Apply one frame without sending anything. */
function applyQuiet(web: WasmHashWeb, frame: Uint8Array): void {
  if (frame.length === 0) return;
  if (frame[0] === ARTIFACT_TAG) web.provideArtifactBytes(frame.subarray(1));
  else if (frame[0] === DELTA_TAG) web.applyDelta(frame);
  else throw new Error(`unexpected notes frame tag 0x${frame[0].toString(16)}`);
}

const str = (v: unknown, d = ''): string => (typeof v === 'string' ? v : d);
const bool = (v: unknown): boolean => v === true;

function parseMeta(v: unknown): GuestMeta | null {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return null;
  const o = v as Record<string, unknown>;
  const agenda = Array.isArray(o.agenda)
    ? o.agenda
        .filter((a): a is Record<string, unknown> => !!a && typeof a === 'object')
        .map((a) => ({ text: str(a.text), covered: bool(a.covered) }))
    : [];
  return {
    title: str(o.title),
    date: str(o.date),
    owner: str(o.owner),
    agenda,
    ended: bool(o.ended),
    ended_at: str(o.ended_at)
  };
}

function parseComment(v: unknown): GuestComment | null {
  if (!v || typeof v !== 'object') return null;
  const o = v as Record<string, unknown>;
  if (typeof o.id !== 'string') return null;
  return {
    id: o.id,
    parent_id: typeof o.parent_id === 'string' ? o.parent_id : null,
    author: str(o.author),
    gid: str(o.gid),
    text: str(o.text),
    quote: str(o.quote),
    mark: str(o.mark),
    created_at: str(o.created_at),
    updated_at: str(o.updated_at),
    resolved: bool(o.resolved),
    deleted: bool(o.deleted),
    detached: bool(o.detached),
    changed: bool(o.changed)
  };
}

function parseThreads(v: unknown): GuestThread[] | null {
  if (!Array.isArray(v)) return null;
  const out: GuestThread[] = [];
  for (const t of v) {
    if (!t || typeof t !== 'object') continue;
    const root = parseComment((t as Record<string, unknown>).root);
    if (!root) continue;
    const rs = (t as Record<string, unknown>).replies;
    const replies = Array.isArray(rs) ? rs.map(parseComment).filter((c): c is GuestComment => c !== null) : [];
    out.push({ root, replies });
  }
  return out;
}

function originOf(o: Record<string, unknown>): string | null {
  return typeof o.origin === 'string' && ORIGIN_HEX.test(o.origin) ? o.origin : null;
}

export class GuestDoc {
  /** Why `status` is 'error'. */
  error: string | null = null;
  /** The room ended (or the page is read-only for another reason). */
  readOnly = false;
  ended = false;
  /** Host times in seconds since 1970, when known. */
  endedAt: number | null = null;
  expires: number | null = null;
  /** The document; null until the first catch-up and while the origin is unknown. */
  doc: Doc | null = null;
  /** Bumped on every change that came from the room: the editor re-renders on it. */
  version = 0;
  meta: GuestMeta | null = null;
  threads: GuestThread[] = [];
  /** Connections in the room (the host's count, ours included). */
  present = 0;
  lastEdit: { name: string; at: number } | null = null;
  you: { gid: string; name: string };
  /** Envelopes that could not be opened (wrong key, altered, unknown content). */
  skipped = 0;
  /** Notes frames handed to the room (for tests and diagnostics). */
  sentFrames = 0;
  /** Bumped when a (re)connect's replay has been merged: the comments panel resends then. */
  catchUps = 0;

  onRemoteChange: (() => void) | undefined;
  wrapRemote: ((apply: () => void) => void) | undefined;
  onGuestComment: ((op: Record<string, unknown>) => void) | undefined;

  readonly room: RoomClient;
  private readonly roomId: string;
  private readonly keys: GuestKeys;
  private readonly url: string;
  private readonly now: () => number;
  private origin: string | null;
  private web: WasmHashWeb | null = null;
  private mirror: WasmHashWeb | null = null;
  /** The room generation whose replay has been merged into `web`. */
  private caughtGen = -1;
  /** Sealed notes envelopes sent and not acknowledged → the raw frame. */
  private inflight = new Map<Uint8Array, Uint8Array>();
  /** Every artifact frame we minted. */
  private artifacts: Uint8Array[] = [];
  private inChain: Promise<void> = Promise.resolve();
  private outChain: Promise<void> = Promise.resolve();
  private people = new Map<string, Person>();
  private listeners = new Set<() => void>();
  private syncWaiters: (() => void)[] = [];
  private drainTimer: ReturnType<typeof setTimeout> | null = null;
  private dirtySince: number | null = null;
  private typingTimer: ReturnType<typeof setTimeout> | null = null;
  private typingSent = false;
  private presenceTimer: ReturnType<typeof setInterval> | null = null;
  private destroyed = false;

  constructor(opts: GuestDocOptions) {
    this.roomId = opts.room;
    this.keys = opts.keys;
    this.you = { gid: opts.gid, name: opts.name };
    this.url = opts.url ?? roomUrl(opts.room);
    this.now = opts.now ?? Date.now;
    this.origin = opts.origin && ORIGIN_HEX.test(opts.origin) ? opts.origin : null;
    this.onRemoteChange = opts.onRemoteChange;
    this.wrapRemote = opts.wrapRemote;
    this.onGuestComment = opts.onGuestComment;
    if (opts.onChange) this.listeners.add(opts.onChange);
    this.room = new RoomClient(
      {
        onWelcome: (w) => this.onWelcome(w),
        onSnapshot: (_seq, env) => this.enqueue(() => this.onSnapshot(env)),
        onFrame: (_seq, env) => {
          const catching = !this.room.caughtUp;
          const gen = this.room.generation;
          this.enqueue(() => this.onFrame(env, catching, gen));
        },
        onPresence: (env) => this.enqueue(() => this.onPresence(env)),
        onCount: (n) => {
          this.present = n;
          this.notify();
        },
        onAck: (_seq, env) => {
          const raw = env ? this.inflight.get(env) : undefined;
          if (env) this.inflight.delete(env);
          if (raw) this.enqueue(() => applyQuiet(this.ensureMirror(), raw));
          if (this.inflight.size === 0) for (const done of this.syncWaiters.splice(0)) done();
        },
        onCaughtUp: () => {
          const gen = this.room.generation;
          this.enqueue(() => this.finishCatchUp(gen));
        },
        onEnded: (endedAt, expires) => this.markEnded(endedAt, expires),
        onState: (s) => this.onRoomState(s)
      },
      { webSocket: opts.webSocket, probe: opts.probe }
    );
  }

  // ---- state ---------------------------------------------------------------------------------

  get status(): GuestStatus {
    if (this.error) return 'error';
    const r = this.room.state;
    if (r === 'gone') return 'gone';
    if (r === 'full') return 'full';
    if (this.ended && this.web) return 'ended';
    if (r === 'live' || r === 'ended') {
      if (this.caughtGen === this.room.generation) return this.ended ? 'ended' : 'live';
      return 'catching_up';
    }
    return this.web ? 'reconnecting' : 'connecting';
  }

  /** Who else is here (presence heard in the last 30 s), the owner first. */
  get others(): Person[] {
    this.sweep();
    return [...this.people.values()].sort((a, b) =>
      a.gid === '' ? -1 : b.gid === '' ? 1 : a.name.localeCompare(b.name)
    );
  }

  /** Names of the others typing now. */
  get typing(): string[] {
    return this.others.filter((p) => p.typing).map((p) => p.name);
  }

  /** Called after anything observable changed. Returns the unsubscribe. */
  subscribe(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  // ---- lifecycle ---------------------------------------------------------------------------------

  connect(): void {
    if (this.destroyed) return;
    this.room.connect(this.url, this.keys.roomToken);
    if (!this.presenceTimer) this.presenceTimer = setInterval(() => this.presenceTick(), PRESENCE_MS);
  }

  /** Rejoin now, keeping the document (what was typed meanwhile is sent after the catch-up). */
  reconnect(): void {
    if (this.destroyed || this.error) return;
    this.room.reconnect();
  }

  /** Stop for good: close the socket, cancel the timers. The document stays readable. */
  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    this.stopTyping();
    this.clearTimers();
    this.room.close();
    for (const done of this.syncWaiters.splice(0)) done();
  }

  // ---- calls from the editor -------------------------------------------------------------------

  /** As NotesDocState's: change the document from outside the editor, then send it. */
  applyLocal<T>(fn: (doc: Doc) => T): T | undefined {
    const doc = this.doc;
    if (!doc || this.readOnly) return undefined;
    const out = this.remote(() => fn(doc));
    this.localChange();
    this.changed();
    return out;
  }

  /** The editor changed the document: send it soon (60 ms debounce, 1 s at most). */
  localChange(): void {
    if (!this.canSend()) return; // a reconnect drains it
    const now = Date.now();
    if (this.dirtySince === null) this.dirtySince = now;
    if (this.drainTimer) clearTimeout(this.drainTimer);
    const wait = Math.min(DEBOUNCE_MS, Math.max(0, MAX_WAIT_MS - (now - this.dirtySince)));
    this.drainTimer = setTimeout(() => this.flush(), wait);
  }

  /** Drain and send now. */
  flush(): void {
    if (this.drainTimer) clearTimeout(this.drainTimer);
    this.drainTimer = null;
    this.dirtySince = null;
    const web = this.web;
    if (!web || !this.canSend()) return;
    this.sendNotes(this.track(drain(web)), this.room.generation);
  }

  /**
   * Send what is pending and resolve once the room acknowledged all of it, or after
   * `timeoutMs`, or at once when not live. For a comment on a selection: the bridge marks the
   * characters, so it must have them.
   */
  async synced(timeoutMs = 2000): Promise<void> {
    this.flush();
    await this.outChain;
    if (!this.canSend() || this.inflight.size === 0) return;
    await new Promise<void>((resolve) => {
      const done = () => {
        clearTimeout(timer);
        resolve();
      };
      const timer = setTimeout(() => {
        const i = this.syncWaiters.indexOf(done);
        if (i >= 0) this.syncWaiters.splice(i, 1);
        resolve();
      }, timeoutMs);
      this.syncWaiters.push(done);
    });
  }

  /** Resolves when every envelope received so far is applied and every send sealed. */
  async idle(): Promise<void> {
    for (;;) {
      const a = this.inChain;
      const b = this.outChain;
      await Promise.all([a, b]);
      if (a === this.inChain && b === this.outChain) return;
    }
  }

  /**
   * The user is (not) typing. `true` tells the room at once and then only keeps the idle timer
   * fresh; `false` goes out after 2 s without a `true`, or immediately when passed.
   */
  setTyping(on: boolean): void {
    if (this.readOnly || this.destroyed) return;
    if (this.typingTimer) clearTimeout(this.typingTimer);
    this.typingTimer = null;
    if (on) {
      if (!this.typingSent) {
        this.typingSent = true;
        this.sendPresence();
      }
      this.typingTimer = setTimeout(() => {
        this.typingTimer = null;
        this.stopTyping();
      }, TYPING_IDLE_MS);
    } else this.stopTyping();
  }

  /** A new display name: used for the next frames and announced now. */
  setName(name: string): void {
    this.you = { ...this.you, name };
    this.sendPresence();
    this.notify();
  }

  /**
   * A comment op for the bridge (create/reply/edit/delete), sealed as COMMENT with our gid and
   * name. Queued while reconnecting. True when it went out now.
   */
  sendComment(op: Record<string, unknown>): Promise<boolean> {
    if (!this.you.name) return Promise.resolve(false); // nothing is said without a name
    const obj = { ...op, gid: this.you.gid, name: this.you.name };
    let sent = false;
    this.outChain = this.outChain.then(async () => {
      if (this.ended || this.destroyed) return;
      const env = await sealJson(this.keys, this.roomId, Kind.COMMENT, obj);
      sent = this.room.sendFrame(env);
    });
    return this.outChain.then(() => sent);
  }

  // ---- incoming --------------------------------------------------------------------------------

  private enqueue(step: () => void | Promise<void>): void {
    this.inChain = this.inChain.then(async () => {
      if (this.error) return;
      try {
        await step();
      } catch (e) {
        this.fail(e instanceof Error ? e.message : String(e));
      }
    });
  }

  private ensureMirror(): WasmHashWeb {
    if (!this.mirror) this.mirror = new WasmHashWeb();
    return this.mirror;
  }

  private onWelcome(w: Welcome): void {
    // Frames sent on the old socket are either in the replay or will be drained again.
    this.inflight.clear();
    for (const done of this.syncWaiters.splice(0)) done();
    this.present = w.present;
    this.expires = w.expires || this.expires;
    if (w.ended) this.markEnded(w.ended_at, w.expires || null);
    else this.sendPresence();
    this.notify();
  }

  private async onSnapshot(env: Uint8Array): Promise<void> {
    let obj: unknown;
    try {
      const { kind, plaintext } = await unseal(this.keys, this.roomId, env);
      if (kind !== Kind.SNAPSHOT) throw new Error('not a snapshot');
      obj = JSON.parse(new TextDecoder().decode(plaintext));
    } catch {
      throw new Error('The notes could not be opened');
    }
    if (!obj || typeof obj !== 'object') throw new Error('The notes could not be opened');
    const o = obj as Record<string, unknown>;
    if (o.format !== 1) throw new Error('These notes come from a newer version of Mnemosyne');
    const mirror = this.ensureMirror();
    const notes = typeof o.notes === 'string' ? o.notes : '';
    if (notes) mirror.mergeEncoded(b64urlDecode(notes));
    if (Array.isArray(o.frames)) for (const f of o.frames) if (typeof f === 'string' && f) applyQuiet(mirror, b64urlDecode(f));
    const origin = originOf(o);
    if (origin && !this.origin) this.origin = origin;
    const threads = parseThreads(o.threads);
    if (threads) this.threads = threads;
    const meta = parseMeta(o.meta);
    if (meta) this.setMeta(meta, originOf((o.meta ?? {}) as Record<string, unknown>));
    this.openIfOrigin();
    this.notify();
  }

  private async onFrame(env: Uint8Array, catching: boolean, gen: number): Promise<void> {
    let opened: { kind: Kind; plaintext: Uint8Array };
    try {
      opened = await unseal(this.keys, this.roomId, env);
    } catch {
      this.skipped++;
      return;
    }
    const { kind, plaintext } = opened;
    if (kind === Kind.NOTES) {
      let frame: Uint8Array, name: string;
      try {
        ({ frame, name } = parseNotes(plaintext));
      } catch {
        this.skipped++;
        return;
      }
      try {
        applyQuiet(this.ensureMirror(), frame);
      } catch {
        this.skipped++; // a frame hashseq cannot read: someone with the link sent garbage
        return;
      }
      const web = this.web;
      if (catching || !web) return; // merged into `web` when the catch-up finishes
      const { send, delivered } = this.remote(() => applyRemote(web, frame));
      this.sendNotes(this.track(send), gen);
      if (delivered > 0) {
        this.lastEdit = { name, at: this.now() };
        this.changed();
      }
      return;
    }
    let obj: unknown;
    try {
      obj = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(plaintext));
    } catch {
      this.skipped++;
      return;
    }
    if (!obj || typeof obj !== 'object') return;
    const o = obj as Record<string, unknown>;
    if (kind === Kind.META) {
      const meta = parseMeta(o);
      if (meta) this.setMeta(meta, originOf(o));
      this.openIfOrigin();
      this.notify();
    } else if (kind === Kind.COMMENT) {
      if (o.op === 'threads') {
        const threads = parseThreads(o.threads);
        if (threads) {
          this.threads = threads;
          this.notify();
        }
      } else this.onGuestComment?.(o);
    }
  }

  private async onPresence(env: Uint8Array): Promise<void> {
    let obj: unknown;
    try {
      const { kind, plaintext } = await unseal(this.keys, this.roomId, env);
      if (kind !== Kind.PRESENCE) throw new Error('not presence');
      obj = JSON.parse(new TextDecoder().decode(plaintext));
    } catch {
      this.skipped++;
      return;
    }
    if (!obj || typeof obj !== 'object') return;
    const o = obj as Record<string, unknown>;
    if (typeof o.gid !== 'string' || o.gid === this.you.gid) return;
    this.people.set(o.gid, { gid: o.gid, name: str(o.name), typing: bool(o.typing), at: this.now() });
    this.notify();
  }

  /** The room's replay for generation `gen` is all in the mirror: bring `web` in line. */
  private finishCatchUp(gen: number): void {
    const mirror = this.ensureMirror();
    let web = this.web;
    if (!web) {
      web = WasmHashWeb.decode(mirror.encode());
      web.applyFrontier(web.encodeFrontier());
      this.web = web;
      this.openIfOrigin();
    } else {
      const w = web;
      // Merge what the room holds, sending nothing; then its clock is exactly the mirror's.
      this.remote(() => w.mergeEncoded(mirror.encode()));
      w.applyFrontier(mirror.encodeFrontier());
    }
    this.caughtGen = gen;
    this.catchUps++;
    if (this.canSend()) {
      const resend = this.artifacts.filter((a) => !mirror.hasArtifactBytes(a.subarray(1)));
      this.sendNotes([...resend, ...this.track(drain(web))], gen);
    }
    this.changed();
  }

  private setMeta(meta: GuestMeta, origin: string | null): void {
    this.meta = meta;
    if (origin && !this.origin) this.origin = origin;
    if (meta.ended) this.markEnded(null, null);
  }

  private openIfOrigin(): void {
    if (this.doc || !this.web || !this.origin) return;
    this.doc = openDoc(this.web, this.origin);
    this.version++;
  }

  private markEnded(endedAt: number | null, expires: number | null): void {
    this.ended = true;
    this.readOnly = true;
    if (endedAt !== null) this.endedAt = endedAt;
    if (expires !== null) this.expires = expires;
    if (this.drainTimer) clearTimeout(this.drainTimer);
    this.drainTimer = null;
    this.dirtySince = null;
    if (this.typingTimer) clearTimeout(this.typingTimer);
    this.typingTimer = null;
    this.typingSent = false;
    this.notify();
  }

  private onRoomState(_s: RoomState): void {
    if (this.room.state !== 'live' && this.room.state !== 'ended') {
      if (this.drainTimer) clearTimeout(this.drainTimer);
      this.drainTimer = null;
      this.dirtySince = null;
      this.typingSent = false;
    }
    this.notify();
  }

  // ---- outgoing --------------------------------------------------------------------------------

  /** May we send notes frames now? Joined, caught up on this socket, writable. */
  private canSend(): boolean {
    return (
      this.web !== null &&
      !this.readOnly &&
      !this.ended &&
      !this.destroyed &&
      this.room.live &&
      this.caughtGen === this.room.generation
    );
  }

  /** Seal and send in order; dropped when the socket changed meanwhile (the reconnect resends). */
  private sendNotes(frames: Uint8Array[], gen: number): void {
    if (!frames.length) return;
    const { gid } = this.you;
    for (const frame of frames) {
      this.outChain = this.outChain.then(async () => {
        const env = await sealNotes(this.keys, this.roomId, gid, this.you.name, frame);
        if (this.room.generation !== gen || !this.canSend()) return;
        this.inflight.set(env, frame);
        if (this.room.sendFrame(env, { queue: false })) this.sentFrames++;
        else this.inflight.delete(env);
      });
    }
    this.outChain = this.outChain.catch((e) => this.fail(e instanceof Error ? e.message : String(e)));
  }

  /** Remember artifact frames so a reconnect can send them again if the room lacks them. */
  private track(frames: Uint8Array[]): Uint8Array[] {
    for (const f of frames) if (f[0] === ARTIFACT_TAG) this.artifacts.push(f);
    return frames;
  }

  private sendPresence(): void {
    // Nobody is announced without a name: a first visit joins the room (to read it) before the
    // name card is answered; `setName` announces then.
    if (this.ended || this.destroyed || !this.you.name) return;
    const obj = { gid: this.you.gid, name: this.you.name, typing: this.typingSent };
    this.outChain = this.outChain
      .then(async () => {
        if (!this.room.live || this.ended) return;
        const env = await sealJson(this.keys, this.roomId, Kind.PRESENCE, obj);
        if (this.room.live && !this.ended) this.room.sendPresence(env, { queue: false });
      })
      .catch(() => {});
  }

  private stopTyping(): void {
    if (this.typingTimer) clearTimeout(this.typingTimer);
    this.typingTimer = null;
    if (!this.typingSent) return;
    this.typingSent = false;
    this.sendPresence();
  }

  private presenceTick(): void {
    if (this.room.live && !this.ended) this.sendPresence();
    if (this.sweep()) this.notify();
  }

  /** Forget people not heard from for PRESENCE_TTL_MS. True if anyone went. */
  private sweep(): boolean {
    const cutoff = this.now() - PRESENCE_TTL_MS;
    let gone = false;
    for (const [gid, p] of this.people) {
      if (p.at < cutoff) {
        this.people.delete(gid);
        gone = true;
      }
    }
    return gone;
  }

  // ---- helpers ---------------------------------------------------------------------------------

  /** Run a change the room makes to the document inside the editor's wrapper, if any. */
  private remote<T>(fn: () => T): T {
    if (!this.doc || !this.wrapRemote) return fn();
    let out: T | undefined;
    this.wrapRemote(() => {
      out = fn();
    });
    return out as T;
  }

  private changed(): void {
    this.version++;
    this.onRemoteChange?.();
    this.notify();
  }

  private notify(): void {
    for (const fn of this.listeners) {
      try {
        fn();
      } catch {
        /* a listener's problem */
      }
    }
  }

  private fail(message: string): void {
    if (this.error) return;
    this.error = message;
    this.clearTimers();
    this.room.close();
    this.notify();
  }

  private clearTimers(): void {
    for (const t of [this.drainTimer, this.typingTimer]) if (t) clearTimeout(t);
    if (this.presenceTimer) clearInterval(this.presenceTimer);
    this.drainTimer = this.typingTimer = null;
    this.presenceTimer = null;
    this.dirtySince = null;
  }
}
