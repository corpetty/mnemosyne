import {
  addComment,
  deleteComment,
  editComment,
  listComments,
  reopenComment,
  resolveComment
} from '$lib/api/backend.js';
import type { CommentCreate, CommentThread } from '$lib/types/index.js';

const BLOCKS = new Set(['decision', 'question', 'item']);

/** Comment threads of the open meeting. */
class CommentsState {
  sessionId = $state<string | null>(null);
  threads = $state<CommentThread[]>([]);
  canComment = $state(false);
  error = $state('');
  #seq = 0;

  /** Threads on transcript segments, by segment index. */
  bySegment = $derived.by(() => {
    const m = new Map<number, CommentThread[]>();
    for (const t of this.threads) {
      const i = t.root.segment_idx;
      if (i == null || t.root.detached) continue;
      m.set(i, [...(m.get(i) ?? []), t]);
    }
    return m;
  });

  /** Threads on summary decisions, questions and action items, by the block's id. */
  byBlock = $derived.by(() => {
    const m = new Map<string, CommentThread[]>();
    for (const t of this.threads) {
      if (!BLOCKS.has(t.root.target) || !t.root.target_id || t.root.detached) continue;
      m.set(t.root.target_id, [...(m.get(t.root.target_id) ?? []), t]);
    }
    return m;
  });

  /** Threads on the live notes, by comment id (the id of the mark `comment:<id>` in the document). */
  byNotesMark = $derived.by(() => {
    const m = new Map<string, CommentThread>();
    for (const t of this.threads) if (t.root.target === 'notes' && !t.root.deleted) m.set(t.root.id, t);
    return m;
  });

  /** Ids of the notes threads still open: only their marks are highlighted. */
  openNotesIds = $derived(
    new Set([...this.byNotesMark.values()].filter((t) => !t.root.resolved_at).map((t) => t.root.id))
  );

  /** Threads on the meeting as a whole. */
  body = $derived(
    this.threads.filter(
      (t) =>
        !t.root.detached &&
        t.root.segment_idx == null &&
        t.root.target !== 'notes' &&
        !(BLOCKS.has(t.root.target) && t.root.target_id)
    )
  );

  /** Threads whose anchor no longer exists. */
  detached = $derived(this.threads.filter((t) => t.root.detached));

  /** Threads still open: roots neither resolved nor deleted. */
  openCount = $derived(this.threads.filter((t) => !t.root.resolved_at && !t.root.deleted).length);

  async load(sessionId: string | null) {
    const seq = ++this.#seq;
    if (sessionId !== this.sessionId) {
      this.sessionId = sessionId;
      this.threads = [];
      this.canComment = false;
    }
    if (!sessionId) return;
    try {
      const r = await listComments(sessionId);
      if (seq !== this.#seq || this.sessionId !== sessionId) return; // opened another meeting meanwhile
      this.threads = r.threads;
      this.canComment = r.can_comment;
      this.error = '';
    } catch (e) {
      if (seq !== this.#seq) return;
      this.error = e instanceof Error ? e.message : 'Could not load comments';
    }
  }

  async #run(action: (sessionId: string) => Promise<unknown>) {
    const id = this.sessionId;
    if (!id) return;
    try {
      await action(id);
      this.error = '';
    } finally {
      await this.load(id);
    }
  }

  add(input: CommentCreate) {
    return this.#run((id) => addComment(id, input));
  }
  reply(rootId: string, text: string) {
    return this.#run((id) => addComment(id, { parent_id: rootId, text } as CommentCreate));
  }
  edit(cid: string, text: string) {
    return this.#run((id) => editComment(id, cid, text));
  }
  remove(cid: string) {
    return this.#run((id) => deleteComment(id, cid));
  }
  resolve(cid: string) {
    return this.#run((id) => resolveComment(id, cid));
  }
  reopen(cid: string) {
    return this.#run((id) => reopenComment(id, cid));
  }
}

export const commentsState = new CommentsState();
