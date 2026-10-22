// Comments on the live notes, seen from the document: what a selection is as a comment
// (its text and the ids of its first and last character, which the server marks, so a reader
// whose editor is read-only can comment too) and where a comment's mark is now.
import { COMMENT_PREFIX, CELL_SEP, cpLen, cpSlice, spansOf, type Doc } from './model.js';

export interface CommentSelection {
  /** Code-point range of the selected text, line breaks at the edges left out. */
  from: number;
  to: number;
  /** The selected text, cells separated by a tab: the quote of the thread. */
  text: string;
  /** Element ids (hex) of the first and the last selected character. */
  first: string;
  last: string;
}

/**
 * The selection `[from, to)` (offsets in either order) as something to comment on, or null when
 * there is no text in it. Leading and trailing line breaks (a triple click takes the whole
 * line, break included) and blanks are left out.
 */
export function commentSelection(doc: Doc, a: number, b: number): CommentSelection | null {
  const text = doc.web.text(doc.obj);
  const len = cpLen(text);
  let from = Math.max(0, Math.min(a, b));
  let to = Math.min(len, Math.max(a, b));
  const at = (i: number) => cpSlice(text, i, i + 1);
  while (from < to && /\s/.test(at(from))) from++;
  while (to > from && /\s/.test(at(to - 1))) to--;
  if (from >= to) return null;
  const first = doc.web.seqIdAt(doc.obj, from);
  const last = doc.web.seqIdAt(doc.obj, to - 1);
  if (!first || !last) return null;
  return { from, to, text: cpSlice(text, from, to).split(CELL_SEP).join('\t'), first, last };
}

/** Where comment `id`'s mark is in the document, `[start, end)` in code points; null when no
 *  character carries it (its text was deleted, or the mark never arrived). */
export function commentRange(doc: Doc, id: string): { start: number; end: number } | null {
  const kind = COMMENT_PREFIX + id;
  let start = -1;
  let end = -1;
  for (const s of spansOf(doc)) {
    if (!s.marks.has(kind)) continue;
    if (start < 0) start = s.start;
    end = s.end;
  }
  return start < 0 ? null : { start, end };
}

/** The comment ids that mark the character at `pos`. */
export function commentsAt(doc: Doc, pos: number): string[] {
  for (const s of spansOf(doc)) {
    if (pos < s.start || pos >= s.end) continue;
    return [...s.marks.keys()].filter((k) => k.startsWith(COMMENT_PREFIX)).map((k) => k.slice(COMMENT_PREFIX.length));
  }
  return [];
}
