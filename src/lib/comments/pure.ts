// Comment threads in pages outside the app, the pure parts shared by the guest page
// (guest/src/comments.ts) and the publication viewer (viewer/src/comments.ts): ids, how threads
// are ordered, and the queue of ops sent but not yet seen in a threads update. Imports nothing
// from the app (the guest and viewer entries bundle it).
//
// Each page mints its comment ids (12 lowercase hex, as the app's own ids) so a resent op is
// recognised by the owner's bridge: a `create` with a known id is not duplicated. An op stays
// "pending" (shown at once) until a threads update reflects it.

/** An op is sent at most this many times in all (the guest page's reconnect retries). */
export const MAX_TRIES = 3;

/** What every page's comments have in common. */
export interface BaseComment {
  id: string;
  parent_id: string | null;
  author: string;
  text: string;
  created_at: string;
  updated_at: string;
  resolved: boolean;
  deleted: boolean;
  detached: boolean;
  changed: boolean;
}

export interface BaseThread<C extends BaseComment = BaseComment> {
  root: C;
  replies: C[];
}

/** What every page's ops have in common. */
export interface BaseOp {
  op: 'create' | 'reply' | 'edit' | 'delete';
  id: string;
  parent_id?: string | null;
  text?: string | null;
}

/** 12 lowercase hex characters. */
export function mintId(random: (n: number) => Uint8Array = (n) => crypto.getRandomValues(new Uint8Array(n))): string {
  return Array.from(random(6), (b) => b.toString(16).padStart(2, '0')).join('');
}

export const ID_RE = /^[0-9a-f]{12}$/;

const time = (iso: string): number => {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? 0 : t;
};

/** A thread nobody can read any more: its root and all replies are deleted. */
export function isGone(t: BaseThread): boolean {
  return t.root.deleted && t.replies.every((r) => r.deleted);
}

/**
 * Threads to show: open ones first, then resolved, each oldest first; deleted-through ones left
 * out unless `keepGone` (the viewer shows them as "removed").
 */
export function orderThreads<T extends BaseThread>(threads: readonly T[], keepGone = false): { open: T[]; resolved: T[] } {
  const live = keepGone ? [...threads] : threads.filter((t) => !isGone(t));
  const byAge = (a: T, b: T) => time(a.root.created_at) - time(b.root.created_at);
  return {
    open: live.filter((t) => !t.root.resolved).sort(byAge),
    resolved: live.filter((t) => t.root.resolved).sort(byAge)
  };
}

/** The comment with this id in `threads`, root or reply. */
export function findComment<C extends BaseComment>(threads: readonly BaseThread<C>[], id: string): C | undefined {
  let found: C | undefined;
  for (const t of threads) {
    if (t.root.id === id) found = t.root;
    else found = t.replies.find((r) => r.id === id) ?? found;
  }
  return found;
}

/** Does `threads` show the effect of `op`? */
export function reflected(op: BaseOp, threads: readonly BaseThread[]): boolean {
  const found = findComment(threads, op.id);
  switch (op.op) {
    case 'create':
    case 'reply':
      return !!found;
    case 'edit':
      return !!found && (found.deleted || found.text === op.text);
    case 'delete':
      return !found || found.deleted;
  }
}

export interface Pending<O extends BaseOp = BaseOp> {
  op: O;
  /** Times it has been sent. */
  tries: number;
  /** When the user made it (ISO), shown as the comment's time. */
  at: string;
}

/** Ops sent and not yet seen in a threads update. */
export class PendingOps<O extends BaseOp = BaseOp, P extends Pending<O> = Pending<O>> {
  list: P[] = [];

  /** `isReflected`: does a threads update show the op (default `reflected`)? */
  constructor(private readonly isReflected: (p: P, threads: readonly BaseThread[]) => boolean = (p, ts) => reflected(p.op, ts)) {}

  /** Remember an op that is being sent (the first try); `extra` goes on the entry. */
  add(op: O, at: string, extra?: Omit<P, keyof Pending<O>>): P {
    const p = { ...(extra ?? {}), op, tries: 1, at } as P;
    this.list.push(p);
    return p;
  }

  /** A threads update arrived: forget what it shows. True if anything went. */
  settle(threads: readonly BaseThread[]): boolean {
    const before = this.list.length;
    this.list = this.list.filter((p) => !this.isReflected(p, threads));
    return this.list.length !== before;
  }

  /**
   * After a reconnect's catch-up (call `settle` with the latest threads first): the ops to send
   * again. Ops already sent MAX_TRIES times are dropped.
   */
  retry(): O[] {
    this.list = this.list.filter((p) => p.tries < MAX_TRIES);
    for (const p of this.list) p.tries++;
    return this.list.map((p) => p.op);
  }

  /** Forget one op (its send was refused locally, or the user took it back). */
  drop(id: string, op?: O['op']): void {
    this.list = this.list.filter((p) => !(p.op.id === id && (!op || p.op.op === op)));
  }
}

/** A comment as shown: `pending` = not in a threads update yet. */
export type ViewOf<C extends BaseComment> = C & { pending: boolean };

export interface ViewThreadOf<C extends BaseComment> {
  root: ViewOf<C>;
  replies: ViewOf<C>[];
}

/**
 * The latest threads with the pending ops applied on top, so a comment shows the moment it is
 * sent. `fresh` makes the comment a pending create or reply shows as (its `parent` is null for a
 * create); edits and deletes change the comment shown.
 */
export function withPendingBy<C extends BaseComment, O extends BaseOp, P extends Pending<O>>(
  threads: readonly BaseThread<C>[],
  pending: readonly P[],
  fresh: (p: P, parent: string | null) => C
): ViewThreadOf<C>[] {
  const view = (c: C, isPending = false): ViewOf<C> => ({ ...c, pending: isPending });
  const out: ViewThreadOf<C>[] = threads.map((t) => ({ root: view(t.root), replies: t.replies.map((r) => view(r)) }));
  const find = (id: string): ViewOf<C> | undefined => findComment(out, id);
  // Creates and replies first (in the order they were made), then edits and deletes of what is shown.
  for (const p of pending) {
    if (p.op.op === 'create' && !find(p.op.id)) out.push({ root: view(fresh(p, null), true), replies: [] });
  }
  for (const p of pending) {
    if (p.op.op !== 'reply' || find(p.op.id)) continue;
    const parent = p.op.parent_id ?? '';
    const t = out.find((x) => x.root.id === parent);
    if (t) t.replies.push(view(fresh(p, parent), true));
  }
  for (const p of pending) {
    const c = find(p.op.id);
    if (!c || c.pending) continue;
    if (p.op.op === 'edit') {
      c.text = p.op.text ?? '';
      c.pending = true;
    } else if (p.op.op === 'delete') {
      c.deleted = true;
      c.text = '';
      c.pending = true;
    }
  }
  return out;
}

/** "just now", "5 min ago", "3 h ago", "2 d ago". */
export function ago(iso: string, now = Date.now()): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '';
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 45) return 'just now';
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

/** Was the comment changed after it was written (more than 2 s later)? */
export function wasEdited(c: Pick<BaseComment, 'created_at' | 'updated_at'>): boolean {
  return time(c.updated_at) - time(c.created_at) > 2000;
}
