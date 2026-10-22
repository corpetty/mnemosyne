import { describe, expect, it } from 'vitest';
import { commentsState } from './comments.svelte.js';
import type { Comment, CommentThread } from '../types/index.js';

function thread(id: string, extra: Partial<Comment> = {}): CommentThread {
  return {
    root: {
      id,
      session_id: 's1',
      parent_id: '',
      target: 'notes',
      target_id: '',
      anchor: { mark: `comment:${id}` },
      quote: 'launch plan',
      text: 'Why?',
      author_id: '',
      author: 'Me',
      mentions: [],
      created_at: '2026-10-22T10:00:00',
      updated_at: '2026-10-22T10:00:00',
      resolved_at: null,
      resolved_by: '',
      deleted: false,
      changed: false,
      detached: false,
      segment_idx: null,
      at_seconds: null,
      notes_range: [4, 15],
      can_edit: true,
      can_delete: true,
      can_resolve: true,
      ...extra
    },
    replies: []
  } as CommentThread;
}

describe('notes threads', () => {
  it('indexes the threads on the notes by comment id; only open ones are highlighted', () => {
    commentsState.threads = [
      thread('open1'),
      thread('done', { resolved_at: '2026-10-22T11:00:00' }),
      thread('gone', { deleted: true }),
      thread('lost', { detached: true, notes_range: null }),
      thread('seg', { target: 'segment', segment_idx: 3 })
    ];
    expect([...commentsState.byNotesMark.keys()]).toEqual(['open1', 'done', 'lost']);
    // A detached thread's mark may still exist in the document: it is open, so it shows.
    expect([...commentsState.openNotesIds].sort()).toEqual(['lost', 'open1']);
    // Notes threads are not "the meeting as a whole" threads, nor transcript ones.
    expect(commentsState.body.map((t) => t.root.id)).toEqual([]);
    expect(commentsState.bySegment.get(3)?.length).toBe(1);
    commentsState.threads = [];
  });
});
