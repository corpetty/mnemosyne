// Primitive edits over the notes seq that return their inverses (undo.ts applies them).
// Everything here is in code-point offsets. Inverses never hold offsets: a removed range
// remembers its neighbours' element ids and is re-inserted next to them; an insert or a
// mark change remembers the element ids it touched (APP_NOTES 20: ids are stable under
// concurrent edits, offsets are not).
import {
  ALIGN,
  BLOCK,
  FLAG,
  LANG,
  cpLen,
  isClosedKind,
  resolveKind,
  spansOf,
  type Doc,
  type Span
} from './model.js';

/** A mark over `[from, to)` of some text, offsets in code points relative to that text. */
export interface MarkSpan {
  from: number;
  to: number;
  kind: string;
  value: string;
}

export type Inverse =
  /** Remove these elements (whichever of them are still live). */
  | { op: 'removeIds'; ids: string[] }
  /**
   * Re-insert `text` with `marks` after element `before`, else before `after`, else at the
   * end. `ids` are the elements the text had, so the undo stack can alias them to the new ones.
   */
  | { op: 'insert'; before: string | null; after: string | null; text: string; marks: MarkSpan[]; ids: string[] }
  /** Set `kind` to `value` (null = unmark) over the live runs of `ids`. */
  | { op: 'marks'; kind: string; ids: string[]; value: string | null };

/**
 * Element ids re-created by an undo/redo map to their replacements: a redo of a later edit
 * whose anchors were undone and redone still finds them (`UndoStack` owns one).
 */
export type Aliases = Map<string, string>;

export function resolveId(aliases: Aliases | undefined, id: string): string {
  if (!aliases) return id;
  for (let i = 0; i < 64; i++) {
    const next = aliases.get(id);
    if (next === undefined) return id;
    id = next;
  }
  return id;
}

/** Element ids of the characters in `[from, to)`. */
export function idsOf(doc: Doc, from: number, to: number): string[] {
  const ids: string[] = [];
  for (let i = from; i < to; i++) {
    const id = doc.web.seqIdAt(doc.obj, i);
    if (id) ids.push(id);
  }
  return ids;
}

/** The contiguous runs `[from, to)` the still-live elements of `ids` occupy now, ascending. */
export function liveRuns(doc: Doc, ids: string[], aliases?: Aliases): [number, number][] {
  const positions: number[] = [];
  for (const id of ids) {
    const p = doc.web.seqPositionOf(doc.obj, resolveId(aliases, id));
    if (p !== undefined) positions.push(p);
  }
  positions.sort((a, b) => a - b);
  const runs: [number, number][] = [];
  for (const p of positions) {
    const last = runs[runs.length - 1];
    if (last && last[1] === p) last[1] = p + 1;
    else if (!last || p > last[1]) runs.push([p, p + 1]);
  }
  return runs;
}

/** The value a kind resolves to on one character (null = unmarked). */
export function resolveValue(kind: string, values: string[] | undefined): string | null {
  if (!values || values.length === 0) return null;
  if (kind === BLOCK) return resolveKind(values);
  return values[0];
}

/** Mark `[from, to)` with the anchor style the kind wants (closed or expanding). */
export function markKind(doc: Doc, kind: string, value: string, from: number, to: number): void {
  if (from >= to) return;
  if (isClosedKind(kind)) doc.web.markRangeClosed(doc.obj, from, to, kind, value);
  else doc.web.markRange(doc.obj, from, to, kind, value);
}

/** Insert plain text; returns the new elements' ids. */
export function insertPlain(doc: Doc, at: number, text: string): string[] {
  if (!text) return [];
  doc.web.textInsert(doc.obj, at, text);
  return idsOf(doc, at, at + cpLen(text));
}

/** Insert text and re-apply marks; returns the new elements' ids. */
export function insertWithMarks(doc: Doc, at: number, text: string, marks: MarkSpan[]): string[] {
  const ids = insertPlain(doc, at, text);
  for (const m of marks) markKind(doc, m.kind, m.value, at + m.from, at + m.to);
  return ids;
}

/** The resolved marks over `[from, to)`, coalesced, offsets relative to `from`. */
export function captureMarks(spans: Span[], from: number, to: number): MarkSpan[] {
  const out: MarkSpan[] = [];
  for (const s of spans) {
    const a = Math.max(s.start, from);
    const b = Math.min(s.end, to);
    if (a >= b) continue;
    for (const [kind, values] of s.marks) {
      const value = resolveValue(kind, values);
      if (value === null) continue;
      const last = out.find((m) => m.kind === kind && m.value === value && m.to === a - from);
      if (last) last.to = b - from;
      else out.push({ from: a - from, to: b - from, kind, value });
    }
  }
  return out;
}

/** Remove `[from, to)`, returning the inverse that re-inserts it next to its neighbours. */
export function removeRange(doc: Doc, from: number, to: number): Inverse {
  const len = doc.web.textLen(doc.obj);
  const text = Array.from(doc.web.text(doc.obj)).slice(from, to).join('');
  const marks = captureMarks(spansOf(doc), from, to);
  const ids = idsOf(doc, from, to);
  const before = from > 0 ? (doc.web.seqIdAt(doc.obj, from - 1) ?? null) : null;
  const after = to < len ? (doc.web.seqIdAt(doc.obj, to) ?? null) : null;
  doc.web.textRemove(doc.obj, from, to - from);
  return { op: 'insert', before, after, text, marks, ids };
}

/**
 * Set `kind` over `[from, to)` to `value` (null = unmark), unless that changes nothing
 * (QUEUE 53: no-op toggles still cost an op and grow the overwrites). Returns one inverse per
 * run of the previous state.
 */
export function applyMark(doc: Doc, kind: string, value: string | null, from: number, to: number): Inverse[] {
  if (from >= to) return [];
  const spans = spansOf(doc);
  // Segments of the previous state: [start, end, previous value, conflicted].
  const segments: { from: number; to: number; value: string | null; conflict: boolean }[] = [];
  for (const s of spans) {
    const a = Math.max(s.start, from);
    const b = Math.min(s.end, to);
    if (a >= b) continue;
    const values = s.marks.get(kind);
    const prev = resolveValue(kind, values);
    const conflict = (values?.length ?? 0) > 1;
    const last = segments[segments.length - 1];
    if (last && last.to === a && last.value === prev && last.conflict === conflict) last.to = b;
    else segments.push({ from: a, to: b, value: prev, conflict });
  }
  const unchanged = segments.every((s) => s.value === value && !s.conflict);
  if (unchanged) return [];
  if (value === null) {
    if (segments.every((s) => s.value === null)) return [];
    doc.web.unmarkRange(doc.obj, from, to, kind);
  } else markKind(doc, kind, value, from, to);
  return segments.map((s) => ({ op: 'marks', kind, ids: idsOf(doc, s.from, s.to), value: s.value }));
}

/** Apply one inverse; returns the inverses of what it did (the redo entry's material). */
export function applyInverse(doc: Doc, inv: Inverse, aliases?: Aliases): Inverse[] {
  switch (inv.op) {
    case 'removeIds': {
      const out: Inverse[] = [];
      for (const [a, b] of liveRuns(doc, inv.ids, aliases).reverse()) out.push(removeRange(doc, a, b));
      return out;
    }
    case 'insert': {
      let at: number | undefined;
      if (inv.before !== null) {
        const p = doc.web.seqPositionOf(doc.obj, resolveId(aliases, inv.before));
        if (p !== undefined) at = p + 1;
      }
      if (at === undefined && inv.after !== null) at = doc.web.seqPositionOf(doc.obj, resolveId(aliases, inv.after));
      if (at === undefined) at = doc.web.textLen(doc.obj);
      const ids = insertWithMarks(doc, at, inv.text, inv.marks);
      if (aliases && inv.ids.length === ids.length) {
        for (let i = 0; i < ids.length; i++) if (inv.ids[i] !== ids[i]) aliases.set(inv.ids[i], ids[i]);
      }
      return [{ op: 'removeIds', ids }];
    }
    case 'marks': {
      const out: Inverse[] = [];
      for (const [a, b] of liveRuns(doc, inv.ids, aliases)) out.push(...applyMark(doc, inv.kind, inv.value, a, b));
      return out;
    }
  }
}

/** Terminator-level marks (block, lang, align) of a line, read from its spans. */
export function terminatorMarks(spans: Span[], end: number): { kind: string | null; lang: string | null; align: string | null } {
  const s = spans.find((x) => end >= x.start && end < x.end);
  return {
    kind: resolveValue(BLOCK, s?.marks.get(BLOCK)),
    lang: resolveValue(LANG, s?.marks.get(LANG)),
    align: resolveValue(ALIGN, s?.marks.get(ALIGN))
  };
}

export { FLAG };
