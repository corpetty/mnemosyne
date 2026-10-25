// What the shared notes editor (NotesEditor.svelte) needs from the page that mounts it: the
// desktop's SharedNotes.svelte (NotesDocState, toasts, the comment stores) and the guest page's
// Notes.svelte (GuestDoc, its notice line). Everything here and in this folder imports only from
// src/lib/notes, so the guest Vite entry can bundle it; app-only behaviour comes in through these
// objects. Fields are read when needed: a host built with getters over reactive state keeps the
// editor up to date (Svelte tracks the reads).
import type { CommentSelection } from '../comments.js';
import type { Chip } from '../chips.js';
import type { Doc, LineKind } from '../model.js';

/** A selection as element ids: the id of the character before each end, null = start of doc. */
export interface SelectionIds {
  a: string | null;
  f: string | null;
}

/** Someone else's caret or selection, as the overlay draws it (phase 11, task 14). */
export interface CaretView {
  /** The member's key (`m<8 hex>` per notes connection, `g:<gid>` for a guest). */
  key: string;
  name: string;
  /**
   * Who the person is for the colour (ui/carets.ts `caretColor` hashes it): a team member's user
   * id, else their name; a guest's gid; on the guest page an owner-side entry's NAME. One person's
   * two windows therefore share a colour.
   */
  who: string;
  sel: SelectionIds;
}

/** Live carets: ours goes out with `setSel`, the others' come back through `carets`. */
export interface PresenceSource {
  /** Our selection moved (null: the editor lost focus). */
  setSel(sel: SelectionIds | null): void;
  carets(): CaretView[];
  /** Called when `carets()` changed; returns the unsubscribe. */
  subscribe(fn: () => void): () => void;
}

/** What the `/` menu, `@` and `[[` can insert (phase 11, task 4); every part optional. */
export interface InsertProvider {
  /** The moment in the recording now, or null when there is none. */
  time?(): { part: number; seconds: number; label: string } | null;
  people?(q: string): Promise<{ name: string; hint?: string }[]>;
  meetings?(q: string): Promise<{ id: string; label: string }[]>;
  onChipClick?(chip: Chip): void;
}

/** The editor's handlers around a remote change (installed at each mount, see `bindRemote`). */
export interface RemoteHooks {
  /** Runs the remote change's `apply` keeping the selection by element id. */
  wrap(apply: () => void): void;
  /** After the remote change was rendered. */
  after(): void;
}

export interface NotesHost {
  /** The document; null while it loads. The editor mounts again when its identity changes. */
  readonly doc: Doc | null;
  readonly readOnly: boolean;
  /** May the selection be commented on (also in a read-only editor)? */
  readonly canComment: boolean;
  /** Shows the Comment button as pressed (a comment is being written). */
  readonly commentActive?: boolean;
  /** Without a selection the Comment button is disabled (default), or not shown at all. */
  readonly hideCommentUntilSelected?: boolean;
  /** Undo/Redo buttons in the toolbar (the guest page: phones have no Ctrl+Z). */
  readonly undoButtons: boolean;
  /** Ids of the open comment threads: their marks are highlighted and clickable. */
  readonly openComments: ReadonlySet<string>;
  /** After every local change (the document store drains and sends). */
  localChange(): void;
  setTyping(on: boolean): void;
  /** Send what is pending now (blur, unmount). */
  flush(): void;
  /** Install the remote hooks on the document store; returns their removal. */
  bindRemote(hooks: RemoteHooks): () => void;
  /** Start a comment on this selection. */
  compose(sel: CommentSelection): void;
  /** A click on a highlighted mark: the open threads on it, first one first. */
  onMark(ids: string[]): void;
  /** A message for the person (the app's toasts, the guest page's notice line). */
  say(message: string, level?: 'info' | 'error'): void;
  presence?: PresenceSource;
  inserts?: InsertProvider;
}

/** What the toolbar shows as on: the caret's line kind (a done task counts as a task) and marks. */
export interface ToolbarActive {
  kind: LineKind | null;
  bold: boolean;
  italic: boolean;
}
