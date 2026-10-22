// The notes document model over one hashseq seq (phase 6 design,
// docs/plans/2026-10-12-hashseq-collaboration.md): the text is lines ending in "\n" (the doc
// always ends with one), a line's kind is a closed `block` mark on its terminating "\n",
// inline formatting is marks on the characters, table cells are separated by U+001F.
//
// Ideas ported by name from hashseq's web/kb.js and APP_NOTES.md (MIT, David Rusu): the
// code-point/UTF-16 helpers (cpLen, cpIndex, utf16Index, cpSlice), flags from markedSpans
// (styledSpans/flagsAt), per-comment mark kinds (APP_NOTES 11), and selection anchored to
// element ids (seqIdAt/seqPositionOf, APP_NOTES 20). Index unit is the Unicode code point
// everywhere the wasm API is concerned; JS strings are UTF-16, hence the helpers.
import type { WasmHashWeb } from './pkg/notes_wasm.js';

export interface Doc {
  web: WasmHashWeb;
  obj: string;
}

// ---- mark vocabulary --------------------------------------------------------------------

export const BLOCK = 'block';
export const LANG = 'lang';
export const ALIGN = 'align';
export const COMMENT_PREFIX = 'comment:';
/** The value of a flag mark (bold, code, comments): MARKS.md's `value_id(true)`. */
export const FLAG = 'true';
/** Separates the cells of a table row. */
export const CELL_SEP = '\u001f';

export const LINE_KINDS = [
  'h1',
  'h2',
  'h3',
  'bullet',
  'number',
  'task',
  'task_done',
  'quote',
  'code',
  'table',
  'table_head'
] as const;
export type LineKind = (typeof LINE_KINDS)[number];

export const INLINE_KINDS = ['bold', 'italic', 'strike', 'code', 'link'] as const;
export type InlineKind = (typeof INLINE_KINDS)[number];

/** Kinds whose end anchor expands (typing at the edge continues the formatting). */
export const EXPANDING_KINDS: ReadonlySet<string> = new Set(['bold', 'italic', 'strike']);
export const LIST_KINDS: ReadonlySet<string> = new Set(['bullet', 'number', 'task', 'task_done']);
export const TABLE_KINDS: ReadonlySet<string> = new Set(['table', 'table_head']);
export const HEADING_KINDS: ReadonlySet<string> = new Set(['h1', 'h2', 'h3']);

export type Align = 'left' | 'center' | 'right' | null;

export function isLineKind(v: string): v is LineKind {
  return (LINE_KINDS as readonly string[]).includes(v);
}

export function isClosedKind(kind: string): boolean {
  return !EXPANDING_KINDS.has(kind);
}

/**
 * The one rule for concurrent `block` values that stay live together (MVR): `task` with
 * `task_done` is done; otherwise the first known kind in markedSpans order (deterministic on
 * every replica).
 */
export function resolveKind(values: readonly string[]): LineKind | null {
  if (values.includes('task_done') && values.includes('task')) return 'task_done';
  for (const v of values) if (isLineKind(v)) return v;
  return null;
}

// ---- code points vs UTF-16 ------------------------------------------------------------------

/** Code-point count of a string (`[...s].length` without the array). */
export function cpLen(s: string): number {
  let n = 0;
  for (const _ of s) n++;
  return n;
}

/** UTF-16 offset to code-point index (a mid-surrogate offset rounds up). */
export function cpIndex(str: string, utf16Idx: number): number {
  return cpLen(str.slice(0, utf16Idx));
}

/** Code-point index to UTF-16 offset (clamped to the string's end). */
export function utf16Index(str: string, cpIdx: number): number {
  let i = 0;
  let n = 0;
  while (n < cpIdx && i < str.length) {
    i += (str.codePointAt(i) ?? 0) > 0xffff ? 2 : 1;
    n++;
  }
  return i;
}

/** `str.slice` in code-point indices. */
export function cpSlice(str: string, a: number, b?: number): string {
  return str.slice(utf16Index(str, a), b == null ? undefined : utf16Index(str, b));
}

// ---- spans ------------------------------------------------------------------------------

/** One coalesced span of markedSpans with its code-point extent and marks by kind. */
export interface Span {
  start: number;
  end: number;
  text: string;
  marks: Map<string, string[]>;
}

interface RawSpan {
  text: string;
  marks: { kind: string; values: string[] }[];
}

export function spansOf(doc: Doc): Span[] {
  const raw = JSON.parse(doc.web.markedSpans(doc.obj)) as RawSpan[];
  const out: Span[] = [];
  let at = 0;
  for (const s of raw) {
    const n = cpLen(s.text);
    const marks = new Map<string, string[]>();
    for (const m of s.marks) marks.set(m.kind, m.values);
    out.push({ start: at, end: at + n, text: s.text, marks });
    at += n;
  }
  return out;
}

/** The span covering code-point `pos`, or null past the end. */
export function spanAt(spans: Span[], pos: number): Span | null {
  for (const s of spans) if (pos >= s.start && pos < s.end) return s;
  return null;
}

/** Live values of `kind` on the character at `pos` (empty when unmarked or out of range). */
export function valuesAt(spans: Span[], pos: number, kind: string): string[] {
  return spanAt(spans, pos)?.marks.get(kind) ?? [];
}

// ---- lines ------------------------------------------------------------------------------

export interface Run {
  text: string;
  bold: boolean;
  italic: boolean;
  strike: boolean;
  code: boolean;
  /** The link URL, or null when none or in conflict. */
  link: string | null;
  /** Several live URLs (concurrent setLink): render as text with "(link conflict)". */
  linkConflict: string[] | null;
  /** Comment ids (the part after `comment:`). */
  comments: string[];
}

export interface Line {
  index: number;
  /** Code-point offset of the first character. */
  start: number;
  /** Code-point offset of the terminating "\n" (the text end for an unterminated last line). */
  end: number;
  /** Element id of the terminator (null when unterminated). */
  id: string | null;
  kind: LineKind | null;
  /** Every live `block` value (more than one = an unresolved concurrent conflict). */
  kinds: string[];
  lang: string | null;
  align: Align[] | null;
  text: string;
  runs: Run[];
  /** Table rows only: the cells, padded to the table's column count. */
  cells: Run[][] | null;
  /** Table rows only: code-point offset of each cell's first character (padded cells: the line end). */
  cellStarts: number[] | null;
}

const EMPTY_RUN = (): Run => ({
  text: '',
  bold: false,
  italic: false,
  strike: false,
  code: false,
  link: null,
  linkConflict: null,
  comments: []
});

function runOf(marks: Map<string, string[]>): Run {
  const r = EMPTY_RUN();
  r.bold = marks.has('bold');
  r.italic = marks.has('italic');
  r.strike = marks.has('strike');
  r.code = marks.has('code');
  const links = marks.get('link');
  if (links && links.length === 1) r.link = links[0];
  else if (links && links.length > 1) r.linkConflict = [...links];
  for (const k of marks.keys()) if (k.startsWith(COMMENT_PREFIX)) r.comments.push(k.slice(COMMENT_PREFIX.length));
  r.comments.sort();
  return r;
}

function sameStyle(a: Run, b: Run): boolean {
  return (
    a.bold === b.bold &&
    a.italic === b.italic &&
    a.strike === b.strike &&
    a.code === b.code &&
    a.link === b.link &&
    String(a.linkConflict) === String(b.linkConflict) &&
    a.comments.join(',') === b.comments.join(',')
  );
}

/** Decode an `align` mark value: one letter per column, `l`/`c`/`r`, `-` = default. */
export function decodeAlign(value: string): Align[] {
  return [...value].map((c) => (c === 'l' ? 'left' : c === 'c' ? 'center' : c === 'r' ? 'right' : null));
}

export function encodeAlign(aligns: readonly Align[]): string {
  return aligns.map((a) => (a === 'left' ? 'l' : a === 'center' ? 'c' : a === 'right' ? 'r' : '-')).join('');
}

/**
 * The document as lines: kind, terminator id, inline runs, table cells. One wasm call for
 * the spans and one `seqIdAt` per line.
 */
export function lines(doc: Doc): Line[] {
  return linesFromSpans(doc, spansOf(doc));
}

export function linesFromSpans(doc: Doc, spans: Span[]): Line[] {
  const out: Line[] = [];
  let runs: Run[] = [];
  let start = 0;
  let pos = 0;
  const push = (terminator: Map<string, string[]> | null) => {
    const kinds = terminator?.get(BLOCK) ?? [];
    const lang = terminator?.get(LANG)?.[0] ?? null;
    const alignRaw = terminator?.get(ALIGN)?.[0] ?? null;
    out.push({
      index: out.length,
      start,
      end: pos,
      id: terminator ? (doc.web.seqIdAt(doc.obj, pos) ?? null) : null,
      kind: resolveKind(kinds),
      kinds,
      lang,
      align: alignRaw === null ? null : decodeAlign(alignRaw),
      text: runs.map((r) => r.text).join(''),
      runs,
      cells: null,
      cellStarts: null
    });
    runs = [];
  };
  for (const s of spans) {
    const style = runOf(s.marks);
    for (const ch of s.text) {
      if (ch === '\n') {
        push(s.marks);
        pos++;
        start = pos;
        continue;
      }
      const last = runs[runs.length - 1];
      if (last && sameStyle(last, style)) last.text += ch;
      else runs.push({ ...style, text: ch, comments: [...style.comments] });
      pos++;
    }
  }
  if (pos > start) push(null);
  splitTables(out);
  return out;
}

/** Split table rows into cells and pad ragged rows to the widest row of their table. */
function splitTables(all: Line[]): void {
  let i = 0;
  while (i < all.length) {
    if (!all[i].kind || !TABLE_KINDS.has(all[i].kind as string)) {
      i++;
      continue;
    }
    let j = i;
    while (j < all.length && all[j].kind && TABLE_KINDS.has(all[j].kind as string)) j++;
    const rows = all.slice(i, j);
    let columns = 1;
    for (const row of rows) {
      const { cells, starts } = cellsOf(row);
      row.cells = cells;
      row.cellStarts = starts;
      columns = Math.max(columns, cells.length);
    }
    for (const row of rows) {
      while ((row.cells as Run[][]).length < columns) {
        (row.cells as Run[][]).push([]);
        (row.cellStarts as number[]).push(row.end);
      }
    }
    i = j;
  }
}

function cellsOf(line: Line): { cells: Run[][]; starts: number[] } {
  const cells: Run[][] = [[]];
  const starts = [line.start];
  let pos = line.start;
  for (const run of line.runs) {
    let text = '';
    const flush = () => {
      if (text) cells[cells.length - 1].push({ ...run, text, comments: [...run.comments] });
      text = '';
    };
    for (const ch of run.text) {
      if (ch === CELL_SEP) {
        flush();
        cells.push([]);
        starts.push(pos + 1);
      } else text += ch;
      pos++;
    }
    flush();
  }
  return { cells, starts };
}

/** The line containing code-point `pos` (`pos === line.end` is the end of that line). */
export function lineAt(all: Line[], pos: number): Line {
  for (const l of all) if (pos <= l.end) return l;
  return all[all.length - 1];
}

/** Plain text of a cell (its runs joined). */
export function cellText(cell: Run[]): string {
  return cell.map((r) => r.text).join('');
}

// ---- invariants -----------------------------------------------------------------------------

/** The text as an array of code points (one entry per seq element). */
export function chars(doc: Doc): string[] {
  return Array.from(doc.web.text(doc.obj));
}

/**
 * Append the terminating "\n" when the doc lacks one (empty, or a remote delete took it).
 * Returns true when it inserted. Two replicas doing this from the same state author the same
 * element (ids are content hashes), so it converges.
 */
export function ensureTerminated(doc: Doc): boolean {
  const text = doc.web.text(doc.obj);
  if (text.endsWith('\n')) return false;
  doc.web.textInsert(doc.obj, doc.web.textLen(doc.obj), '\n');
  return true;
}

/** Open (idempotently) the notes seq at `originHex`. */
export function openDoc(web: WasmHashWeb, originHex: string): Doc {
  return { web, obj: web.createSeq(originHex) };
}
