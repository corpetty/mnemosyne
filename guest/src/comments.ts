// Comments on the guest page, the pure parts: ids, the ops the bridge takes, how threads are
// ordered, and the queue of ops sent but not yet seen in a `threads` update.
//
// The page mints every comment id (12 lowercase hex, as the app's own ids) so a resent op is
// recognised by the bridge: a `create` with a known id is not duplicated or marked twice. An op
// stays "pending" (shown at once, "Sending…") until a `threads` update reflects it. After a
// reconnect's catch-up the ones still not reflected go out again, at most MAX_TRIES sends in all.
import type { GuestComment, GuestThread } from './doc';

/** An op is sent at most this many times in all; after that the next reconnect drops it. */
export const MAX_TRIES = 3;

export type GuestOp =
  | { op: 'create'; id: string; parent_id: ''; text: string; quote: string; notes_ids: [string, string] }
  | { op: 'reply'; id: string; parent_id: string; text: string }
  | { op: 'edit'; id: string; text: string }
  | { op: 'delete'; id: string };

/** What a selection in the notes is as a comment (the part of notes/comments.ts' CommentSelection used). */
export interface Anchor {
  text: string;
  first: string;
  last: string;
}

/** 12 lowercase hex characters. */
export function mintId(random: (n: number) => Uint8Array = (n) => crypto.getRandomValues(new Uint8Array(n))): string {
  return Array.from(random(6), (b) => b.toString(16).padStart(2, '0')).join('');
}

export const ID_RE = /^[0-9a-f]{12}$/;

export function createOp(id: string, text: string, anchor: Anchor): GuestOp {
  return { op: 'create', id, parent_id: '', text, quote: anchor.text, notes_ids: [anchor.first, anchor.last] };
}
export function replyOp(id: string, parentId: string, text: string): GuestOp {
  return { op: 'reply', id, parent_id: parentId, text };
}
export function editOp(id: string, text: string): GuestOp {
  return { op: 'edit', id, text };
}
export function deleteOp(id: string): GuestOp {
  return { op: 'delete', id };
}

/** Is this comment the page's own (a guest's comments carry their gid)? */
export function isMine(c: Pick<GuestComment, 'gid'>, gid: string): boolean {
  return gid !== '' && c.gid === gid;
}

const time = (iso: string): number => {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? 0 : t;
};

/** A thread nobody can read any more: its root and all replies are deleted. */
export function isGone(t: GuestThread): boolean {
  return t.root.deleted && t.replies.every((r) => r.deleted);
}

/** Threads to show: open ones first, then resolved, each oldest first; deleted-through ones left out. */
export function orderThreads<T extends GuestThread>(threads: readonly T[]): { open: T[]; resolved: T[] } {
  const live = threads.filter((t) => !isGone(t));
  const byAge = (a: T, b: T) => time(a.root.created_at) - time(b.root.created_at);
  return {
    open: live.filter((t) => !t.root.resolved).sort(byAge),
    resolved: live.filter((t) => t.root.resolved).sort(byAge)
  };
}

/** Ids of the threads whose mark is painted in the notes: open and not deleted. */
export function openMarkIds(threads: readonly GuestThread[]): string[] {
  return threads.filter((t) => !t.root.resolved && !t.root.deleted).map((t) => t.root.id);
}

/** Does `threads` show the effect of `op`? */
export function reflected(op: GuestOp, threads: readonly GuestThread[]): boolean {
  let found: GuestComment | undefined;
  for (const t of threads) {
    if (t.root.id === op.id) found = t.root;
    else found = t.replies.find((r) => r.id === op.id) ?? found;
  }
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

export interface Pending {
  op: GuestOp;
  /** Times it has been sent. */
  tries: number;
  /** When the user made it (ISO), shown as the comment's time. */
  at: string;
}

/** Ops sent and not yet seen in a `threads` update. */
export class PendingOps {
  list: Pending[] = [];

  /** Remember an op that is being sent (the first try). */
  add(op: GuestOp, at: string): Pending {
    const p = { op, tries: 1, at };
    this.list.push(p);
    return p;
  }

  /** A `threads` update arrived: forget what it shows. True if anything went. */
  settle(threads: readonly GuestThread[]): boolean {
    const before = this.list.length;
    this.list = this.list.filter((p) => !reflected(p.op, threads));
    return this.list.length !== before;
  }

  /**
   * After a reconnect's catch-up (call `settle` with the latest threads first): the ops to send
   * again. Ops already sent MAX_TRIES times are dropped.
   */
  retry(): GuestOp[] {
    this.list = this.list.filter((p) => p.tries < MAX_TRIES);
    for (const p of this.list) p.tries++;
    return this.list.map((p) => p.op);
  }

  /** Forget one op (its send was refused locally, or the user took it back). */
  drop(id: string, op?: GuestOp['op']): void {
    this.list = this.list.filter((p) => !(p.op.id === id && (!op || p.op.op === op)));
  }
}

export interface ViewComment extends GuestComment {
  /** Not in a `threads` update yet. */
  pending: boolean;
}
export interface ViewThread {
  root: ViewComment;
  replies: ViewComment[];
}

const view = (c: GuestComment, pending = false): ViewComment => ({ ...c, pending });

/** The latest threads with the pending ops applied on top, so a comment shows the moment it is sent. */
export function withPending(
  threads: readonly GuestThread[],
  pending: readonly Pending[],
  me: { gid: string; name: string }
): ViewThread[] {
  const out: ViewThread[] = threads.map((t) => ({ root: view(t.root), replies: t.replies.map((r) => view(r)) }));
  const fresh = (p: Pending, parent: string | null, text: string, quote: string): ViewComment => ({
    id: p.op.id,
    parent_id: parent,
    author: me.name,
    gid: me.gid,
    text,
    quote,
    mark: '',
    created_at: p.at,
    updated_at: p.at,
    resolved: false,
    deleted: false,
    detached: false,
    changed: false,
    pending: true
  });
  const find = (id: string): ViewComment | undefined => {
    for (const t of out) {
      if (t.root.id === id) return t.root;
      const r = t.replies.find((x) => x.id === id);
      if (r) return r;
    }
    return undefined;
  };
  // Creates and replies first (in the order they were made), then edits and deletes of what is shown.
  for (const p of pending) {
    if (p.op.op === 'create' && !find(p.op.id)) out.push({ root: fresh(p, null, p.op.text, p.op.quote), replies: [] });
  }
  for (const p of pending) {
    if (p.op.op !== 'reply' || find(p.op.id)) continue;
    const parent = p.op.parent_id;
    const t = out.find((x) => x.root.id === parent);
    if (t) t.replies.push(fresh(p, parent, p.op.text, ''));
  }
  for (const p of pending) {
    const c = find(p.op.id);
    if (!c || c.pending) continue;
    if (p.op.op === 'edit') {
      c.text = p.op.text;
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
export function wasEdited(c: Pick<GuestComment, 'created_at' | 'updated_at'>): boolean {
  return time(c.updated_at) - time(c.created_at) > 2000;
}
