// Readers' comments on a live publication (docs/plans/2026-11-05-live-publications.md, M2), the
// pure parts: the COMMENT ops a reader sends, the threads the owner's signed THREADS carries, the
// reader's identity, and what is sent but not yet seen back. No DOM: ReaderComments.svelte and
// Meeting.svelte draw it, live.ts (`Follower.sendComment`) boxes and sends.
//
// - Comments exist only while the shown manifest has `live` and the owner's META says
//   `comments: true` (`commentsOpen`). Threads already received stay readable when it says false.
// - The reader is an Ed25519 key per publication plus a typed name, both in localStorage under
//   keys scoped to the publication id (`ReaderIdentity`); without storage they live in memory for
//   the page's life. `rid` = ridOf(public key); a reader's own comments carry it in THREADS. The
//   private key goes into no URL, frame or log: only `Follower.sendComment` reads it, to sign.
// - Every op is a JSON object with exactly the keys `op, id, parent_id, text, name, at, anchor`
//   (canonical JSON is the box's job): `create` carries the anchor (all six keys, null where
//   unused) and `parent_id: null`; `reply` the root's id as `parent_id` and `anchor: null`; `edit`
//   the new text, `parent_id: null`, `anchor: null`; `delete` `text: null` too. `name` is the
//   reader's current name on every op; `at` the moment the reader made it (ISO, UTC).
// - Pending: an op shows at once ("Sending…") until a THREADS shows it (by id; an edit by its text
//   or by a newer `updated_at`, since the owner's redaction may change the text); after
//   NOT_SENT_MS without it, "Not sent · Retry". Retry sends the same op again (same id and `at`;
//   the bridge is idempotent).
// - Approval (the owner's "Approve comments before other readers see them"): a comment waiting for
//   it is in no reader's THREADS. THREADS' `pending` lists, per rid and by id only, the comments
//   still waiting; this page keeps the words of its own sent comments (`HeldComments`, localStorage
//   with a memory fallback, per publication and comment id) and shows the ones listed for its rid
//   as "Waiting for the owner", until THREADS shows the comment itself or no longer lists it. With
//   the text gone, nothing is shown for it.
import {
  PendingOps,
  findComment,
  mintId,
  orderThreads,
  reflected,
  withPendingBy,
  type BaseComment,
  type BaseOp,
  type BaseThread,
  type Pending,
  type ViewThreadOf
} from '$lib/comments/pure';
import { fromHex, hex } from '$lib/sealed/envelope';
import { newPrivate, parseLiveKeys, ridOf, signPublic } from '$lib/sealed/signed';
import type { Manifest } from './format';

/** The bridge's cap on a comment (characters, as Python counts them: code points). */
export const MAX_TEXT = 4000;
/** The composer shows a counter from this many characters. */
export const COUNT_FROM = 3500;
export const NAME_MAX = 80;
/** An op not seen in a THREADS this long after it was sent is "Not sent". */
export const NOT_SENT_MS = 15_000;
/** Characters of context before and after a summary passage. */
export const CONTEXT_CHARS = 32;

export type OpTarget = '' | 'body' | 'notes' | 'decision' | 'question' | 'item' | 'quote';
export type ItemTarget = 'decision' | 'question' | 'item';

/** Where a new thread goes, as the COMMENT op says it. */
export interface OpAnchor {
  target: OpTarget;
  target_id: string | null;
  quote_id: string | null;
  exact: string | null;
  prefix: string | null;
  suffix: string | null;
}

export interface ReaderOp extends BaseOp {
  op: 'create' | 'reply' | 'edit' | 'delete';
  id: string;
  parent_id: string | null;
  text: string | null;
  name: string;
  at: string;
  anchor: OpAnchor | null;
}

/** Where a thread is, as THREADS says it. */
export interface ThreadAnchor {
  target: string;
  target_id: string | null;
  quote_id: string | null;
  quote: string | null;
}

export interface ReaderComment extends BaseComment {
  /** A reader's comments carry their rid; null for the owner's side. */
  rid: string | null;
  anchor: ThreadAnchor;
  /** This reader's own comment, waiting for the owner's approval: shown from the text kept here. */
  waiting?: boolean;
}
export type ReaderThread = BaseThread<ReaderComment>;

/** What the composer is about: the op's anchor and the line shown above it ("On: …", null for the meeting). */
export interface Draft {
  anchor: OpAnchor;
  line: string | null;
}

/** Comments may be written: the manifest is live and the owner's META says comments are on. */
export function commentsOpen(manifest: Pick<Manifest, 'live'>, meta: { comments: boolean } | null): boolean {
  return parseLiveKeys(manifest.live) !== null && meta?.comments === true;
}

/** Text length as the bridge counts it (code points). */
export function textLength(text: string): number {
  let n = 0;
  for (const _ of text) n++;
  return n;
}

/** May this (trimmed) text be sent? */
export function sendable(text: string): boolean {
  const t = text.trim();
  return t !== '' && textLength(t) <= MAX_TEXT;
}

/** "1 comment", "3 comments". */
export function commentCount(n: number): string {
  return n === 1 ? '1 comment' : `${n} comments`;
}

/** What Meeting.svelte needs to draw the comment affordances (null: none). */
export interface MeetingComments {
  /** Comments on each part, by `anchorKey`. */
  counts: Record<string, number>;
  /** The owner's META says the notes can be commented on (`notes: true`); absent or false: nothing is offered on them. */
  notes?: boolean;
  /** Open the composer anchored to a part. */
  onComment(draft: Draft): void;
  /** Bring the threads on a part into view. */
  onShow(key: string): void;
}

/** A name as we keep and send it: trimmed, inner whitespace collapsed, at most 80 characters. */
export function cleanName(text: string): string {
  return Array.from(text.replace(/\s+/g, ' ').trim()).slice(0, NAME_MAX).join('').trim();
}

// ---- anchors -----------------------------------------------------------------------------

const NONE: OpAnchor = { target: '', target_id: null, quote_id: null, exact: null, prefix: null, suffix: null };

export function meetingAnchor(): OpAnchor {
  return { ...NONE };
}

export function itemAnchor(target: ItemTarget, id: string): OpAnchor {
  return { ...NONE, target, target_id: id };
}

export function quoteAnchor(quoteId: string): OpAnchor {
  return { ...NONE, target: 'quote', quote_id: quoteId };
}

/**
 * A passage of the summary's or the notes' rendered text (`full`, characters `start`..`end`) as
 * an anchor of that `target` (`body`, the default, or `notes`): the passage without its outer whitespace, and up to CONTEXT_CHARS before and after it.
 * Null when only whitespace is selected.
 */
export function passageAnchor(full: string, start: number, end: number, target: 'body' | 'notes' = 'body'): OpAnchor | null {
  let s = Math.max(0, Math.min(start, end));
  let e = Math.min(full.length, Math.max(start, end));
  while (s < e && /\s/.test(full[s])) s++;
  while (e > s && /\s/.test(full[e - 1])) e--;
  if (s >= e) return null;
  return {
    ...NONE,
    target,
    exact: full.slice(s, e),
    prefix: full.slice(Math.max(0, s - CONTEXT_CHARS), s),
    suffix: full.slice(e, e + CONTEXT_CHARS)
  };
}

const ITEM_TARGETS = new Set(['decision', 'question', 'item']);

/**
 * The page element an anchor belongs to (Meeting.svelte's `data-anchor`): `decision:<id>`,
 * `question:<id>`, `item:<id>`, `quote:<id>`, `body`, `notes`; null for the meeting as a whole.
 */
export function anchorKey(a: { target: string; target_id: string | null; quote_id: string | null } | null): string | null {
  if (!a) return null;
  if (a.quote_id) return `quote:${a.quote_id}`;
  if (ITEM_TARGETS.has(a.target) && a.target_id) return `${a.target}:${a.target_id}`;
  if (a.target === 'body' || a.target === 'notes') return a.target;
  return null;
}

/** The text a thread's "On: …" line quotes: the item's text as the manifest shows it, else the anchor's quote. */
export function anchorLine(a: ThreadAnchor, m: Manifest): string | null {
  const key = anchorKey(a);
  if (!key) return null;
  const id = key.slice(key.indexOf(':') + 1);
  const fallback = a.quote?.trim() || null;
  if (key.startsWith('quote:')) return (m.quotes ?? []).find((q) => q.id === id)?.text ?? fallback;
  if (key.startsWith('decision:')) return (m.decisions ?? []).find((d) => d.id === id)?.text ?? fallback;
  if (key.startsWith('question:')) return (m.questions ?? []).find((q) => q.id === id)?.text ?? fallback;
  if (key.startsWith('item:')) return (m.action_items ?? []).find((x) => x.id === id)?.text ?? fallback;
  return fallback;
}

/** The THREADS form of an op's anchor (a pending thread's "On: …" line). */
function threadAnchor(a: OpAnchor | null): ThreadAnchor {
  return {
    target: a?.target ?? '',
    target_id: a?.target_id ?? null,
    quote_id: a?.quote_id ?? null,
    quote: a?.exact ?? null
  };
}

// ---- ops ---------------------------------------------------------------------------------

export function createOp(id: string, text: string, name: string, at: string, anchor: OpAnchor): ReaderOp {
  return { op: 'create', id, parent_id: null, text, name, at, anchor: { ...anchor } };
}
export function replyOp(id: string, parentId: string, text: string, name: string, at: string): ReaderOp {
  return { op: 'reply', id, parent_id: parentId, text, name, at, anchor: null };
}
export function editOp(id: string, text: string, name: string, at: string): ReaderOp {
  return { op: 'edit', id, parent_id: null, text, name, at, anchor: null };
}
export function deleteOp(id: string, name: string, at: string): ReaderOp {
  return { op: 'delete', id, parent_id: null, text: null, name, at, anchor: null };
}

// ---- THREADS -----------------------------------------------------------------------------

const str = (v: unknown): string | null => (typeof v === 'string' ? v : null);

function parseComment(v: unknown): ReaderComment | null {
  if (!v || typeof v !== 'object') return null;
  const o = v as Record<string, unknown>;
  if (typeof o.id !== 'string' || !o.id) return null;
  const a = o.anchor && typeof o.anchor === 'object' ? (o.anchor as Record<string, unknown>) : {};
  return {
    id: o.id,
    parent_id: str(o.parent_id),
    author: str(o.author) ?? '',
    rid: str(o.rid),
    text: str(o.text) ?? '',
    anchor: { target: str(a.target) ?? '', target_id: str(a.target_id), quote_id: str(a.quote_id), quote: str(a.quote) },
    created_at: str(o.created_at) ?? '',
    updated_at: str(o.updated_at) ?? str(o.created_at) ?? '',
    resolved: o.resolved === true,
    deleted: o.deleted === true,
    detached: o.detached === true,
    changed: o.changed === true
  };
}

/** THREADS' `threads` as the page uses them; malformed entries are left out. */
export function parseThreads(raw: readonly unknown[] | null | undefined): ReaderThread[] {
  const out: ReaderThread[] = [];
  for (const t of raw ?? []) {
    if (!t || typeof t !== 'object') continue;
    const o = t as Record<string, unknown>;
    const root = parseComment(o.root);
    if (!root) continue;
    const replies = Array.isArray(o.replies) ? o.replies.map(parseComment).filter((c): c is ReaderComment => !!c) : [];
    out.push({ root, replies });
  }
  return out;
}

// ---- the reader --------------------------------------------------------------------------

/** The storage calls we use (localStorage, or a stand-in in tests). */
export interface KeyValue {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export const storageKeys = (pubId: string) => ({
  name: `mnemosyne.reader.${pubId}.name`,
  key: `mnemosyne.reader.${pubId}.key`
});

const KEY_HEX = /^[0-9a-f]{64}$/;

/** Who this reader is for one publication: a name and an Ed25519 signing key (its 32-byte seed). */
export class ReaderIdentity {
  private memory = new Map<string, string>();
  private readonly keys: { name: string; key: string };

  constructor(
    readonly pubId: string,
    readonly store: () => KeyValue | null = () => globalThis.localStorage ?? null
  ) {
    this.keys = storageKeys(pubId);
  }

  private get(key: string): string | null {
    try {
      const v = this.store()?.getItem(key);
      if (typeof v === 'string') return v;
    } catch {
      /* private mode or blocked storage: memory below */
    }
    return this.memory.get(key) ?? null;
  }

  private set(key: string, value: string): void {
    this.memory.set(key, value);
    try {
      this.store()?.setItem(key, value);
    } catch {
      /* kept in memory for the page's life */
    }
  }

  /** The remembered name, or "" before the first comment. */
  get name(): string {
    return cleanName(this.get(this.keys.name) ?? '');
  }

  set name(value: string) {
    this.set(this.keys.name, cleanName(value));
  }

  /** The signing key, if this reader has one yet. */
  get key(): Uint8Array | null {
    const v = this.get(this.keys.key);
    return v && KEY_HEX.test(v) ? fromHex(v) : null;
  }

  /** The signing key, made on first use and kept from then on. */
  ensureKey(): Uint8Array {
    const have = this.key;
    if (have) return have;
    const key = newPrivate();
    this.set(this.keys.key, hex(key));
    return key;
  }

  /** This reader's rid, or null before they have a key. */
  async rid(): Promise<string | null> {
    const key = this.key;
    return key ? ridOf(signPublic(key)) : null;
  }
}

// ---- the words of comments waiting for approval ----------------------------------------------

/** What this page keeps of a comment it sent, for as long as the owner has not approved it. */
export interface Held {
  parent_id: string | null;
  text: string;
  name: string;
  at: string;
  anchor: ThreadAnchor;
}

export const heldKey = (pubId: string) => `mnemosyne.reader.${pubId}.held`;
/** The newest this many are kept. */
export const MAX_HELD = 50;

function parseHeld(raw: string | null): Record<string, Held> {
  const out: Record<string, Held> = {};
  if (!raw) return out;
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return out;
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return out;
  for (const [id, v] of Object.entries(data as Record<string, unknown>)) {
    if (!v || typeof v !== 'object') continue;
    const o = v as Record<string, unknown>;
    const a = o.anchor && typeof o.anchor === 'object' ? (o.anchor as Record<string, unknown>) : {};
    if (typeof o.text !== 'string' || !o.text || typeof o.name !== 'string' || typeof o.at !== 'string') continue;
    out[id] = {
      parent_id: str(o.parent_id),
      text: o.text,
      name: o.name,
      at: o.at,
      anchor: { target: str(a.target) ?? '', target_id: str(a.target_id), quote_id: str(a.quote_id), quote: str(a.quote) }
    };
  }
  return out;
}

/**
 * The text of this reader's comments that may be waiting for approval, by comment id, in one
 * localStorage entry per publication (memory when storage is blocked). Never sent anywhere.
 */
export class HeldComments {
  private memory: string | null = null;
  private readonly key: string;

  constructor(
    pubId: string,
    private readonly store: () => KeyValue | null = () => globalThis.localStorage ?? null
  ) {
    this.key = heldKey(pubId);
  }

  all(): Record<string, Held> {
    let raw = this.memory;
    try {
      const v = this.store()?.getItem(this.key);
      if (typeof v === 'string') raw = v;
    } catch {
      /* private mode or blocked storage: memory */
    }
    return parseHeld(raw);
  }

  get(id: string): Held | undefined {
    return this.all()[id];
  }

  private save(map: Record<string, Held>): void {
    const ids = Object.keys(map);
    for (const id of ids.slice(0, Math.max(0, ids.length - MAX_HELD))) delete map[id];
    const raw = JSON.stringify(map);
    this.memory = raw;
    try {
      this.store()?.setItem(this.key, raw);
    } catch {
      /* kept in memory for the page's life */
    }
  }

  set(id: string, held: Held): void {
    const map = this.all();
    delete map[id]; // (re-added last: the newest)
    map[id] = held;
    this.save(map);
  }

  delete(id: string): void {
    const map = this.all();
    if (id in map) {
      delete map[id];
      this.save(map);
    }
  }
}

// ---- comments and their pending ops --------------------------------------------------------

export interface ReaderPending extends Pending<ReaderOp> {
  /** When it was last sent (ms). */
  sent: number;
  /** An edit: the comment's `updated_at` when it was made (a THREADS with another one shows it). */
  base: string | null;
}

export type PendingState = 'sending' | 'not_sent';

export type ViewComment = ReaderThread['root'] & { pending: boolean };
export type ViewThread = ViewThreadOf<ReaderComment>;

/** Hands an op to the room: boxed for the owner and signed with `key` (live.ts `Follower.sendComment`). */
export type SendOp = (op: ReaderOp, key: Uint8Array) => unknown;

function shows(p: ReaderPending, threads: readonly BaseThread[]): boolean {
  if (reflected(p.op, threads)) return true;
  if (p.op.op !== 'edit' || p.base === null) return false;
  const c = findComment(threads, p.op.id);
  return !!c && c.updated_at !== p.base;
}

export class ReaderComments {
  threads: ReaderThread[] = [];
  /** This reader's rid once known (after their first op, or at once when they had a key). */
  rid: string | null = null;
  /** THREADS' `pending` of the newest THREADS (null before any): ids waiting for approval, by rid. */
  private waitingBy: Record<string, string[]> | null = null;
  /** THREADS' `blocked`: the rids the owner has stopped taking comments from. */
  private blocked: string[] = [];
  /** An op counts as seen when THREADS shows it, or lists its comment as waiting for approval. */
  readonly pending = new PendingOps<ReaderOp, ReaderPending>(
    (p, threads) => shows(p, threads) || this.waitingIds().has(p.op.id)
  );

  constructor(
    readonly identity: ReaderIdentity,
    private readonly send: SendOp,
    private readonly now: () => number = () => Date.now(),
    private readonly onChange: () => void = () => {},
    private readonly held: HeldComments = new HeldComments(identity.pubId, identity.store)
  ) {}

  /** Learn the rid of a key kept from an earlier visit. */
  async init(): Promise<void> {
    await this.learnRid();
  }

  private async learnRid(): Promise<void> {
    if (this.rid) return;
    try {
      this.rid = await this.identity.rid();
    } catch {
      return; // no WebCrypto: own comments are not recognised, sending still fails gracefully
    }
    if (this.rid) {
      this.dropIfBlocked();
      this.forgetHeld();
      this.onChange();
    }
  }

  /** Is this reader's own rid on the owner's block list (THREADS `blocked`)? */
  get blockedHere(): boolean {
    return this.rid !== null && this.blocked.includes(this.rid);
  }

  /** A blocked reader's ops in flight are dropped, not retried: the bridge would refuse them. */
  private dropIfBlocked(): void {
    if (!this.blockedHere) return;
    for (const p of [...this.pending.list]) this.pending.drop(p.op.id);
  }

  get name(): string {
    return this.identity.name;
  }

  setName(name: string): boolean {
    const clean = cleanName(name);
    if (!clean) return false;
    this.identity.name = clean;
    this.onChange();
    return true;
  }

  /** The newest THREADS' `threads` and `pending` (as live.ts kept them). */
  setThreads(
    raw: readonly unknown[] | null | undefined,
    waiting: Record<string, string[]> | null = null,
    blocked: readonly string[] = []
  ): void {
    this.threads = parseThreads(raw);
    this.waitingBy = waiting ?? {};
    this.blocked = [...blocked];
    this.pending.settle(this.threads);
    this.dropIfBlocked();
    this.forgetHeld();
    this.onChange();
  }

  /** The ids of this reader's comments the newest THREADS lists as waiting for approval. */
  private waitingIds(): Set<string> {
    return new Set(this.rid && this.waitingBy ? (this.waitingBy[this.rid] ?? []) : []);
  }

  /**
   * Forget the words of what THREADS shows itself, or no longer lists as waiting (approved and
   * then hidden, deleted by the owner, ...). Not while an op for it is still on its way, and not
   * before a THREADS has come and this page knows its rid.
   */
  private forgetHeld(): void {
    if (!this.rid || this.waitingBy === null) return;
    const waiting = this.waitingIds();
    for (const id of Object.keys(this.held.all())) {
      const inFlight = this.pending.list.some((p) => p.op.id === id);
      if (findComment(this.threads, id) || (!waiting.has(id) && !inFlight)) this.held.delete(id);
    }
  }

  /** The comments listed as waiting that this page still has the words of, in `all`'s threads. */
  private addWaiting(all: ViewThread[]): void {
    const waiting = this.waitingIds();
    if (!waiting.size) return;
    const held = this.held.all();
    const make = (id: string, h: Held): ViewComment => ({
      id,
      parent_id: h.parent_id,
      author: `${h.name} (reader)`,
      rid: this.rid,
      text: h.text,
      anchor: h.anchor,
      created_at: h.at,
      updated_at: h.at,
      resolved: false,
      deleted: false,
      detached: false,
      changed: false,
      waiting: true,
      pending: false
    });
    const ids = [...waiting].filter((id) => held[id] && !findComment(all, id));
    for (const id of ids) {
      if (!held[id].parent_id) all.push({ root: make(id, held[id]), replies: [] });
    }
    for (const id of ids) {
      const parent = held[id].parent_id;
      const thread = parent ? all.find((t) => t.root.id === parent) : undefined;
      if (thread) thread.replies.push(make(id, held[id]));
    }
  }

  isMine(c: Pick<ReaderComment, 'rid'>): boolean {
    return this.rid !== null && c.rid === this.rid;
  }

  /** "sending" or "not_sent" for a comment with ops in flight; null when it has none. */
  stateOf(id: string): PendingState | null {
    const ps = this.pending.list.filter((p) => p.op.id === id);
    if (!ps.length) return null;
    const t = this.now();
    return ps.some((p) => t - p.sent >= NOT_SENT_MS) ? 'not_sent' : 'sending';
  }

  /** The soonest moment a pending op turns "not sent" (ms), or null. */
  nextDeadline(): number | null {
    const t = this.now();
    const ds = this.pending.list.map((p) => p.sent + NOT_SENT_MS).filter((d) => d > t);
    return ds.length ? Math.min(...ds) : null;
  }

  /** The threads with what is pending on top, open first, then resolved. */
  view(): { open: ViewThread[]; resolved: ViewThread[] } {
    const author = `${this.name} (reader)`;
    const all = withPendingBy<ReaderComment, ReaderOp, ReaderPending>(this.threads, this.pending.list, (p, parent) => ({
      id: p.op.id,
      parent_id: parent,
      author,
      rid: this.rid,
      text: p.op.text ?? '',
      anchor: threadAnchor(p.op.anchor),
      created_at: p.at,
      updated_at: p.at,
      resolved: false,
      deleted: false,
      detached: false,
      changed: false
    }));
    this.addWaiting(all);
    // A deleted comment stays as "removed", a thread deleted through included.
    return orderThreads(all, true);
  }

  /** Comments (not deleted, pending included) on each anchored part, by `anchorKey`. */
  counts(): Record<string, number> {
    const out: Record<string, number> = {};
    const { open, resolved } = this.view();
    for (const t of [...open, ...resolved]) {
      const key = anchorKey(t.root.anchor);
      if (!key) continue;
      const n = [t.root, ...t.replies].filter((c) => !c.deleted).length;
      if (n) out[key] = (out[key] ?? 0) + n;
    }
    return out;
  }

  private at(): string {
    return new Date(this.now()).toISOString();
  }

  private submit(op: ReaderOp, base: string | null = null, keep?: Held): ReaderOp {
    const key = this.identity.ensureKey();
    this.pending.add(op, op.at, { sent: this.now(), base });
    if (keep) this.held.set(op.id, keep); // (the owner may ask to approve it first)
    this.onChange();
    void this.learnRid();
    this.send(op, key);
    return op;
  }

  /** A new thread; null when there is no name yet or the text cannot be sent. */
  create(draft: Draft, text: string): ReaderOp | null {
    const t = text.trim();
    if (this.blockedHere || !this.name || !sendable(t)) return null;
    const op = createOp(mintId(), t, this.name, this.at(), draft.anchor);
    return this.submit(op, null, { parent_id: null, text: t, name: this.name, at: op.at, anchor: threadAnchor(op.anchor) });
  }

  reply(rootId: string, text: string): ReaderOp | null {
    const t = text.trim();
    if (this.blockedHere || !this.name || !sendable(t)) return null;
    const op = replyOp(mintId(), rootId, t, this.name, this.at());
    return this.submit(op, null, { parent_id: rootId, text: t, name: this.name, at: op.at, anchor: threadAnchor(null) });
  }

  /** Edit one's own comment; null when it is not one's own, unchanged or cannot be sent. */
  edit(c: ReaderComment, text: string): ReaderOp | null {
    const t = text.trim();
    if (this.blockedHere || !this.isMine(c) || !sendable(t) || t === c.text) return null;
    // With approval on, the edited text waits again: its words stay here meanwhile.
    const keep = { parent_id: c.parent_id, text: t, name: this.name, at: c.created_at, anchor: c.anchor };
    return this.submit(editOp(c.id, t, this.name, this.at()), c.updated_at, keep);
  }

  /** Delete one's own comment; null when it is not one's own. */
  remove(c: ReaderComment): ReaderOp | null {
    if (this.blockedHere || !this.isMine(c)) return null;
    this.held.delete(c.id);
    return this.submit(deleteOp(c.id, this.name, this.at()));
  }

  /** Send again, unchanged, every op in flight for this comment ("Not sent · Retry"). */
  retry(id: string): ReaderOp[] {
    const key = this.identity.key;
    const ps = this.pending.list.filter((p) => p.op.id === id);
    if (this.blockedHere || !key || !ps.length) return [];
    const t = this.now();
    for (const p of ps) {
      p.sent = t;
      p.tries++;
      this.send(p.op, key);
    }
    this.onChange();
    return ps.map((p) => p.op);
  }
}
