// Local undo/redo over our own edits (phase 6 design: "undo is local, inverse ops of your own
// edits"). An entry is the list of inverses a command returned (ops.ts); undoing applies them
// last to first and the inverses THEY return become the redo entry. Nothing is stored by
// offset: removed text re-inserts next to its neighbours' element ids and marks are restored
// on the elements they covered, so an undo after a remote edit still lands on the right text.
import { ensureTerminated, type Doc } from './model.js';
import { applyInverse, type Aliases, type Inverse } from './ops.js';

export interface UndoEntry {
  inverse: Inverse[];
  /** Caret to restore after undoing (the position before the edit), or null. */
  caret: number | null;
  /** True for a typing burst that later typing may join. */
  typing: boolean;
  at: number;
}

export interface PushOptions {
  /** The caret before the edit (restored by undo as a best effort). */
  caret?: number;
  /** The text this entry typed: consecutive typing is grouped unless a word boundary or `groupMs` passes. */
  typed?: string;
}

export class UndoStack {
  private undos: UndoEntry[] = [];
  private redos: UndoEntry[] = [];
  /** Elements re-created by an undo or redo, old id to new (ops.ts `Aliases`). */
  private aliases: Aliases = new Map();

  constructor(
    private doc: Doc,
    private opts: { groupMs?: number; limit?: number; now?: () => number } = {}
  ) {}

  /** Record a command's inverse. Empty inverses (no-ops) are ignored. */
  push(inverse: Inverse[], opts: PushOptions = {}): void {
    if (inverse.length === 0) return;
    this.redos = [];
    const now = (this.opts.now ?? Date.now)();
    const last = this.undos[this.undos.length - 1];
    const typed = opts.typed;
    if (
      typed !== undefined &&
      last?.typing &&
      now - last.at <= (this.opts.groupMs ?? 1000) &&
      !/^\s/.test(typed)
    ) {
      last.inverse.push(...inverse);
      last.at = now;
      return;
    }
    this.undos.push({ inverse, caret: opts.caret ?? null, typing: typed !== undefined, at: now });
    const limit = this.opts.limit ?? 200;
    if (this.undos.length > limit) this.undos.splice(0, this.undos.length - limit);
  }

  /** Start a new group: the next typed text will not join the previous burst. */
  breakGroup(): void {
    const last = this.undos[this.undos.length - 1];
    if (last) last.typing = false;
  }

  canUndo(): boolean {
    return this.undos.length > 0;
  }

  canRedo(): boolean {
    return this.redos.length > 0;
  }

  /** Undo the last entry; returns the caret to restore, or null when there was nothing. */
  undo(): number | null {
    const entry = this.undos.pop();
    if (!entry) return null;
    const redo = this.apply(entry);
    this.redos.push(redo);
    return entry.caret;
  }

  redo(): number | null {
    const entry = this.redos.pop();
    if (!entry) return null;
    const undo = this.apply(entry);
    this.undos.push(undo);
    return entry.caret;
  }

  private apply(entry: UndoEntry): UndoEntry {
    const inverse: Inverse[] = [];
    for (let i = entry.inverse.length - 1; i >= 0; i--) inverse.push(...applyInverse(this.doc, entry.inverse[i], this.aliases));
    if (ensureTerminated(this.doc)) {
      const id = this.doc.web.seqIdAt(this.doc.obj, this.doc.web.textLen(this.doc.obj) - 1);
      if (id) inverse.push({ op: 'removeIds', ids: [id] });
    }
    return { inverse, caret: entry.caret, typing: false, at: entry.at };
  }

  clear(): void {
    this.undos = [];
    this.redos = [];
    this.aliases = new Map();
  }
}
