// Other people's carets, the pure half (phase 11, task 14): a selection given as element ids
// becomes document offsets, and a person becomes one of eight colours. CaretOverlay.svelte draws
// the result; the status-line dots of both pages use the same palette so a person is one colour
// everywhere. Imports only from src/lib/notes (the guest bundle shares this file).
import type { Doc } from '../model.js';
import type { SelectionIds } from './types.js';

/** Number of caret colours. */
export const CARET_COLORS = 8;

/**
 * A stable colour index for a person: FNV-1a of `who`, mod 8. `who` is what makes two windows
 * of one person agree: a team member's user id (else their name), a guest's gid, and for people
 * on the owner's side as the guest page sees them ("o:<key>" entries) their NAME.
 */
export function caretColor(who: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < who.length; i++) {
    h ^= who.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h % CARET_COLORS;
}

export interface CaretColors {
  /** The caret bar, the name flag's background and the status dot. */
  bar: string;
  /** The name flag's text (dark: every bar colour is light enough for it). */
  text: string;
  /** The selection's fill, translucent over the dark editor. */
  fill: string;
}

const BARS = ['#f87171', '#fb923c', '#facc15', '#4ade80', '#2dd4bf', '#60a5fa', '#a78bfa', '#f472b6'];

/** The eight colours, readable on the dark editor (gray-900): Tailwind's 400 shades. */
export const PALETTE: readonly CaretColors[] = BARS.map((bar) => ({ bar, text: '#0b1020', fill: bar + '40' }));

/** The colours for an index from `caretColor` (any integer is folded into range). */
export function paletteAt(index: number): CaretColors {
  return PALETTE[((Math.trunc(index) % CARET_COLORS) + CARET_COLORS) % CARET_COLORS];
}

/** The colours for a person, from their `who` (see `caretColor`). */
export function colorsFor(who: string): CaretColors {
  return paletteAt(caretColor(who));
}

/** What the offsets need from a document: `seqPositionOf` over the notes object. */
export type AnchorDoc = Pick<Doc, 'web' | 'obj'>;

export interface CaretOffsets {
  anchor: number;
  focus: number;
}

/**
 * Where a selection sent as element ids is in the document now: each end is after the character
 * whose id it carries (null = the start). Ids never move, offsets do, so the same selection is
 * right after any remote insert or delete. An id this document does not have, or whose
 * character was deleted, gives null: the caret is hidden until its owner sends a new position
 * (their editor re-sends when its own caret moved off a deleted character).
 */
export function caretOffsets(doc: AnchorDoc, sel: SelectionIds): CaretOffsets | null {
  const anchor = offsetAfter(doc, sel.a);
  const focus = offsetAfter(doc, sel.f);
  if (anchor === null || focus === null) return null;
  return { anchor, focus };
}

function offsetAfter(doc: AnchorDoc, id: string | null): number | null {
  if (id === null) return 0;
  try {
    const p = doc.web.seqPositionOf(doc.obj, id);
    return p === undefined ? null : p + 1;
  } catch {
    return null; // not an element id of this document (a malformed one from a peer)
  }
}
