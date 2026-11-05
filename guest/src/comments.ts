// Comments on the guest page, the pure parts: ids, the ops the bridge takes, how threads are
// ordered, and the queue of ops sent but not yet seen in a `threads` update. The parts the
// publication viewer shares live in src/lib/comments/pure.ts; this file re-exports them with the
// guest page's types.
//
// The page mints every comment id (12 lowercase hex, as the app's own ids) so a resent op is
// recognised by the bridge: a `create` with a known id is not duplicated or marked twice. An op
// stays "pending" (shown at once, "Sending…") until a `threads` update reflects it. After a
// reconnect's catch-up the ones still not reflected go out again, at most MAX_TRIES sends in all.
import {
  PendingOps as BasePendingOps,
  withPendingBy,
  type Pending as BasePending,
  type ViewOf
} from '$lib/comments/pure';
import type { GuestComment, GuestThread } from './doc';

export { ID_RE, MAX_TRIES, ago, isGone, mintId, orderThreads, reflected, wasEdited } from '$lib/comments/pure';

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

/** Ids of the threads whose mark is painted in the notes: open and not deleted. */
export function openMarkIds(threads: readonly GuestThread[]): string[] {
  return threads.filter((t) => !t.root.resolved && !t.root.deleted).map((t) => t.root.id);
}

export type Pending = BasePending<GuestOp>;

/** Ops sent and not yet seen in a `threads` update. */
export class PendingOps extends BasePendingOps<GuestOp> {}

export type ViewComment = ViewOf<GuestComment>;
export interface ViewThread {
  root: ViewComment;
  replies: ViewComment[];
}

/** The latest threads with the pending ops applied on top, so a comment shows the moment it is sent. */
export function withPending(
  threads: readonly GuestThread[],
  pending: readonly Pending[],
  me: { gid: string; name: string }
): ViewThread[] {
  return withPendingBy<GuestComment, GuestOp, Pending>(threads, pending, (p, parent) => ({
    id: p.op.id,
    parent_id: parent,
    author: me.name,
    gid: me.gid,
    text: 'text' in p.op ? p.op.text : '',
    quote: p.op.op === 'create' ? p.op.quote : '',
    mark: '',
    created_at: p.at,
    updated_at: p.at,
    resolved: false,
    deleted: false,
    detached: false,
    changed: false
  }));
}
