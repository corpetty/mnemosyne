// The pure parts of the guest page's comments: ids, ops, ordering, the pending queue.
import { describe, expect, it } from 'vitest';
import {
  ID_RE,
  MAX_TRIES,
  PendingOps,
  ago,
  createOp,
  deleteOp,
  editOp,
  isMine,
  mintId,
  openMarkIds,
  orderThreads,
  reflected,
  replyOp,
  wasEdited,
  withPending
} from './comments';
import type { GuestComment, GuestThread } from './doc';

function comment(id: string, over: Partial<GuestComment> = {}): GuestComment {
  return {
    id,
    parent_id: null,
    author: 'Olga',
    gid: '',
    text: `text ${id}`,
    quote: 'quoted',
    mark: `comment:${id}`,
    created_at: '2026-10-24T10:00:00+00:00',
    updated_at: '2026-10-24T10:00:00+00:00',
    resolved: false,
    deleted: false,
    detached: false,
    changed: false,
    ...over
  };
}
const thread = (id: string, over: Partial<GuestComment> = {}, replies: GuestComment[] = []): GuestThread => ({
  root: comment(id, over),
  replies
});

describe('ids and ops', () => {
  it('mints 12 lowercase hex characters', () => {
    for (let i = 0; i < 20; i++) expect(mintId()).toMatch(ID_RE);
    expect(mintId(() => Uint8Array.of(0, 1, 15, 16, 171, 255))).toBe('00010f10abff');
    expect(mintId()).not.toBe(mintId());
  });

  it('builds the ops the bridge takes', () => {
    const anchor = { text: 'the plan', first: 'aa', last: 'bb' };
    expect(createOp('0123456789ab', 'why?', anchor)).toEqual({
      op: 'create',
      id: '0123456789ab',
      parent_id: '',
      text: 'why?',
      quote: 'the plan',
      notes_ids: ['aa', 'bb']
    });
    expect(replyOp('ba9876543210', '0123456789ab', 'because')).toEqual({
      op: 'reply',
      id: 'ba9876543210',
      parent_id: '0123456789ab',
      text: 'because'
    });
    expect(editOp('x', 'new')).toEqual({ op: 'edit', id: 'x', text: 'new' });
    expect(deleteOp('x')).toEqual({ op: 'delete', id: 'x' });
  });

  it('knows its own comments by gid, never when the gid is empty', () => {
    expect(isMine(comment('a', { gid: 'g1' }), 'g1')).toBe(true);
    expect(isMine(comment('a', { gid: 'g2' }), 'g1')).toBe(false);
    expect(isMine(comment('a', { gid: '' }), '')).toBe(false);
  });
});

describe('ordering', () => {
  it('puts open threads first, oldest first, resolved last, and leaves out deleted-through ones', () => {
    const ts = [
      thread('late', { created_at: '2026-10-24T12:00:00+00:00' }),
      thread('done', { resolved: true, created_at: '2026-10-24T08:00:00+00:00' }),
      thread('early', { created_at: '2026-10-24T09:00:00+00:00' }),
      thread('gone', { deleted: true, text: '' }),
      thread('kept', { deleted: true, text: '', created_at: '2026-10-24T10:30:00+00:00' }, [comment('r', { parent_id: 'kept' })])
    ];
    const { open, resolved } = orderThreads(ts);
    expect(open.map((t) => t.root.id)).toEqual(['early', 'kept', 'late']);
    expect(resolved.map((t) => t.root.id)).toEqual(['done']);
  });

  it('paints the marks of open, undeleted threads only', () => {
    const ts = [thread('a'), thread('b', { resolved: true }), thread('c', { deleted: true })];
    expect(openMarkIds(ts)).toEqual(['a']);
  });

  it('says how long ago, and whether a comment was edited', () => {
    const t = Date.parse('2026-10-24T10:00:00Z');
    expect(ago('2026-10-24T10:00:00Z', t + 10_000)).toBe('just now');
    expect(ago('2026-10-24T10:00:00Z', t + 5 * 60_000)).toBe('5 min ago');
    expect(ago('2026-10-24T10:00:00Z', t + 3 * 3600_000)).toBe('3 h ago');
    expect(ago('2026-10-24T10:00:00Z', t + 2 * 86400_000)).toBe('2 d ago');
    expect(ago('nonsense')).toBe('');
    expect(wasEdited(comment('a'))).toBe(false);
    expect(wasEdited(comment('a', { updated_at: '2026-10-24T10:05:00+00:00' }))).toBe(true);
  });
});

describe('reflected', () => {
  const ts = [thread('t1', { gid: 'me', text: 'hello' }, [comment('r1', { parent_id: 't1', text: 'hi' })]), thread('t2', { deleted: true })];

  it('creates and replies by id', () => {
    expect(reflected(createOp('t1', 'x', { text: '', first: '', last: '' }), ts)).toBe(true);
    expect(reflected(createOp('nope', 'x', { text: '', first: '', last: '' }), ts)).toBe(false);
    expect(reflected(replyOp('r1', 't1', 'hi'), ts)).toBe(true);
    expect(reflected(replyOp('r9', 't1', 'hi'), ts)).toBe(false);
  });
  it('edits by their text, deletes by the deleted flag', () => {
    expect(reflected(editOp('t1', 'hello'), ts)).toBe(true);
    expect(reflected(editOp('t1', 'changed'), ts)).toBe(false);
    expect(reflected(editOp('t2', 'whatever'), ts)).toBe(true); // deleted meanwhile
    expect(reflected(deleteOp('t2'), ts)).toBe(true);
    expect(reflected(deleteOp('t1'), ts)).toBe(false);
    expect(reflected(deleteOp('missing'), ts)).toBe(true);
  });
});

describe('PendingOps', () => {
  it('forgets an op once a threads update shows it', () => {
    const p = new PendingOps();
    p.add(createOp('t1', 'x', { text: '', first: '', last: '' }), 'now');
    p.add(replyOp('r1', 't1', 'y'), 'now');
    expect(p.settle([])).toBe(false);
    expect(p.list).toHaveLength(2);
    expect(p.settle([thread('t1')])).toBe(true);
    expect(p.list.map((x) => x.op.id)).toEqual(['r1']);
  });

  it('offers what is still unseen after a reconnect, MAX_TRIES sends in all', () => {
    const p = new PendingOps();
    const op = replyOp('r1', 't1', 'y');
    p.add(op, 'now'); // try 1
    const sends: number[] = [];
    for (let i = 0; i < 5; i++) sends.push(p.retry().length);
    expect(sends).toEqual([1, 1, 0, 0, 0]); // tries 2 and 3, then dropped
    expect(MAX_TRIES).toBe(3);
    expect(p.list).toEqual([]);
  });

  it('does not resend what the catch-up showed', () => {
    const p = new PendingOps();
    p.add(createOp('t1', 'x', { text: '', first: '', last: '' }), 'now');
    p.add(deleteOp('t9'), 'now');
    p.settle([thread('t1')]);
    expect(p.retry()).toEqual([]);
  });

  it('drops one op by id', () => {
    const p = new PendingOps();
    p.add(editOp('a', 'x'), 'now');
    p.add(deleteOp('a'), 'now');
    p.drop('a', 'edit');
    expect(p.list.map((x) => x.op.op)).toEqual(['delete']);
    p.drop('a');
    expect(p.list).toEqual([]);
  });
});

describe('withPending', () => {
  const me = { gid: 'me', name: 'Ada' };
  const anchor = { text: 'the plan', first: 'aa', last: 'bb' };

  it('shows a new thread and a reply at once, marked pending', () => {
    const p = new PendingOps();
    p.add(createOp('n1', 'new thread', anchor), '2026-10-24T11:00:00.000Z');
    p.add(replyOp('r1', 'n1', 'to my own'), '2026-10-24T11:00:01.000Z');
    p.add(replyOp('r2', 't1', 'to theirs'), '2026-10-24T11:00:02.000Z');
    const out = withPending([thread('t1')], p.list, me);
    expect(out.map((t) => t.root.id)).toEqual(['t1', 'n1']);
    const n = out[1];
    expect(n.root).toMatchObject({ pending: true, gid: 'me', author: 'Ada', quote: 'the plan', text: 'new thread' });
    expect(n.replies.map((r) => [r.id, r.pending])).toEqual([['r1', true]]);
    expect(out[0].replies.map((r) => [r.id, r.pending, r.parent_id])).toEqual([['r2', true, 't1']]);
    expect(out[0].root.pending).toBe(false);
  });

  it('drops a pending op the threads already show (no double)', () => {
    const p = new PendingOps();
    p.add(createOp('t1', 'x', anchor), 'now');
    const out = withPending([thread('t1', { text: 'x' })], p.list, me);
    expect(out).toHaveLength(1);
    expect(out[0].root.pending).toBe(false);
  });

  it('applies a pending edit and delete over what is shown', () => {
    const p = new PendingOps();
    p.add(editOp('t1', 'edited'), 'now');
    p.add(deleteOp('r1'), 'now');
    const out = withPending([thread('t1', { gid: 'me' }, [comment('r1', { parent_id: 't1', gid: 'me' })])], p.list, me);
    expect(out[0].root).toMatchObject({ text: 'edited', pending: true });
    expect(out[0].replies[0]).toMatchObject({ deleted: true, text: '', pending: true });
  });

  it('does not change the threads it was given', () => {
    const ts = [thread('t1', { text: 'orig' })];
    const p = new PendingOps();
    p.add(editOp('t1', 'edited'), 'now');
    withPending(ts, p.list, me);
    expect(ts[0].root.text).toBe('orig');
  });
});
