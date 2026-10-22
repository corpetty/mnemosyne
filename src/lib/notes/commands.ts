// Editing commands over the notes document (phase 6 design). Each command mutates the seq
// through the wasm API and returns the inverse operations for undo.ts plus the caret the
// editor should show afterwards. Offsets are code points. No DOM here.
//
// Ported by name from hashseq's web/kb.js (MIT, David Rusu): the typed-markup input rules
// (maybeInputRule: convert after the delimiter is in the seq, mark first, then remove the
// delimiters), closed ends for conversions (APP_NOTES 17/18), expanding ends for formatting
// (MARKS.md's anchor table), list continuation on Enter (APP_NOTES 35).
//
// Policies (agreed in the brief):
// - Enter at the END of a line inserts the new "\n" AFTER the old terminator, so the line's
//   kind (and a trailing expanding mark) stay with the line. Bullet, number, task, quote and
//   code lines continue on the new line (task_done continues as task); Enter on an EMPTY such
//   line clears its kind instead. Enter in a table row adds a plain line after the row.
// - Enter in the MIDDLE of a line inserts "\n" at the caret: the old terminator (and its kind)
//   stays with the second half. Headings are re-marked so the FIRST half keeps the heading and
//   the second half is plain (at the line start the heading is pushed down whole); continuing
//   kinds are copied to the first half (both halves keep the kind); `lang` moves to the first
//   half.
// - Deleting one or more terminators joins lines: the merged line takes the kind, lang and
//   align of the line where the deletion starts (re-marked after the join).
// - Backspace at a line start first clears the line's kind; a second Backspace joins.
import {
  ALIGN,
  BLOCK,
  CELL_SEP,
  COMMENT_PREFIX,
  EXPANDING_KINDS,
  FLAG,
  HEADING_KINDS,
  LANG,
  LIST_KINDS,
  TABLE_KINDS,
  cpLen,
  cpSlice,
  encodeAlign,
  ensureTerminated,
  lineAt,
  lines,
  spanAt,
  spansOf,
  type Align,
  type Doc,
  type InlineKind,
  type Line,
  type LineKind
} from './model.js';
import { applyMark, idsOf, insertPlain, markKind, removeRange, type Inverse } from './ops.js';

export interface CommandResult {
  inverse: Inverse[];
  /** Where the caret goes afterwards (code points). */
  caret: number;
}

/** Kinds Enter carries onto the next line. */
const CONTINUING: ReadonlySet<string> = new Set([...LIST_KINDS, 'quote', 'code']);

function continuation(kind: LineKind): LineKind {
  return kind === 'task_done' ? 'task' : kind;
}

/** Keep the doc terminated before a command reads lines; the fix-up is part of the entry. */
function terminate(doc: Doc, inv: Inverse[]): void {
  if (ensureTerminated(doc)) {
    const id = doc.web.seqIdAt(doc.obj, doc.web.textLen(doc.obj) - 1);
    if (id) inv.push({ op: 'removeIds', ids: [id] });
  }
}

function setTerminatorMarks(
  doc: Doc,
  end: number,
  want: { kind: string | null; lang: string | null; align: string | null }
): Inverse[] {
  return [
    ...applyMark(doc, BLOCK, want.kind, end, end + 1),
    ...applyMark(doc, LANG, want.lang, end, end + 1),
    ...applyMark(doc, ALIGN, want.align, end, end + 1)
  ];
}

const alignValue = (line: Line): string | null => (line.align ? encodeAlign(line.align) : null);

// ---- text ----------------------------------------------------------------------------------

export function insertText(doc: Doc, at: number, text: string): CommandResult {
  const ids = insertPlain(doc, at, text);
  return { inverse: ids.length ? [{ op: 'removeIds', ids }] : [], caret: at + cpLen(text) };
}

export function deleteRange(doc: Doc, from: number, to: number): CommandResult {
  if (from >= to) return { inverse: [], caret: from };
  const inv: Inverse[] = [];
  const ls = lines(doc);
  const first = lineAt(ls, from);
  const survivor = to <= ls[ls.length - 1].end ? lineAt(ls, to) : null;
  inv.push(removeRange(doc, from, to));
  terminate(doc, inv);
  const want = { kind: first.kind, lang: first.lang, align: alignValue(first) };
  if (survivor && survivor.index !== first.index) {
    // Joined: the terminator that survives is the later line's; it takes the first line's marks.
    inv.push(...setTerminatorMarks(doc, from + (survivor.end - to), want));
  } else if (!survivor && from > 0) {
    // The range reached the end of the doc: the fresh terminator stands in for the first line's.
    inv.push(...setTerminatorMarks(doc, from, want));
  }
  return { inverse: inv, caret: from };
}

export function splitLine(doc: Doc, at: number): CommandResult {
  const inv: Inverse[] = [];
  terminate(doc, inv);
  const ls = lines(doc);
  const line = lineAt(ls, at);
  if (line.kind && TABLE_KINDS.has(line.kind)) {
    inv.push({ op: 'removeIds', ids: insertPlain(doc, line.end + 1, '\n') });
    return { inverse: inv, caret: line.end + 1 };
  }
  if (at >= line.end) {
    if (line.kind && CONTINUING.has(line.kind)) {
      if (line.start === line.end) {
        inv.push(...setTerminatorMarks(doc, line.end, { kind: null, lang: null, align: null }));
        return { inverse: inv, caret: line.start };
      }
      inv.push({ op: 'removeIds', ids: insertPlain(doc, line.end + 1, '\n') });
      inv.push(...applyMark(doc, BLOCK, continuation(line.kind), line.end + 1, line.end + 2));
      return { inverse: inv, caret: line.end + 1 };
    }
    inv.push({ op: 'removeIds', ids: insertPlain(doc, line.end + 1, '\n') });
    return { inverse: inv, caret: line.end + 1 };
  }
  // Mid-line: the new terminator sits at `at`, the old one moves to line.end + 1. At the
  // line start the first half is empty, so a heading stays with the text (pushed down).
  inv.push({ op: 'removeIds', ids: insertPlain(doc, at, '\n') });
  if (line.kind && HEADING_KINDS.has(line.kind) && at > line.start) {
    inv.push(...applyMark(doc, BLOCK, line.kind, at, at + 1));
    inv.push(...applyMark(doc, BLOCK, null, line.end + 1, line.end + 2));
  } else if (line.kind && CONTINUING.has(line.kind)) {
    inv.push(...applyMark(doc, BLOCK, line.kind, at, at + 1));
  }
  if (line.lang) {
    inv.push(...applyMark(doc, LANG, line.lang, at, at + 1));
    inv.push(...applyMark(doc, LANG, null, line.end + 1, line.end + 2));
  }
  return { inverse: inv, caret: at + 1 };
}

/** Backspace at a line start: clear the line's kind first; a second press joins. */
export function joinWithPrevious(doc: Doc, lineStart: number): CommandResult {
  const ls = lines(doc);
  const line = lineAt(ls, lineStart);
  if (lineStart !== line.start) return deleteRange(doc, lineStart - 1, lineStart);
  if (line.kind || line.lang || line.align) {
    const inv = setTerminatorMarks(doc, line.end, { kind: null, lang: null, align: null });
    return { inverse: inv, caret: line.start };
  }
  if (line.index === 0) return { inverse: [], caret: line.start };
  return deleteRange(doc, line.start - 1, line.start);
}

// ---- line kinds ----------------------------------------------------------------------------

/** Set (or clear, with null) the kind of every line touching `[from, to]`. */
export function setLineKind(doc: Doc, from: number, to: number, kind: LineKind | null): CommandResult {
  const inv: Inverse[] = [];
  terminate(doc, inv);
  for (const line of lines(doc)) {
    if (line.end < from || line.start > to) continue;
    inv.push(...applyMark(doc, BLOCK, kind, line.end, line.end + 1));
    if (kind !== 'code' && line.lang) inv.push(...applyMark(doc, LANG, null, line.end, line.end + 1));
    if ((!kind || !TABLE_KINDS.has(kind)) && line.align) inv.push(...applyMark(doc, ALIGN, null, line.end, line.end + 1));
  }
  return { inverse: inv, caret: from };
}

export function toggleTask(doc: Doc, pos: number): CommandResult {
  const line = lineAt(lines(doc), pos);
  const next: LineKind = line.kind === 'task' ? 'task_done' : 'task';
  return { inverse: applyMark(doc, BLOCK, next, line.end, line.end + 1), caret: pos };
}

// ---- inline -------------------------------------------------------------------------------

/** Bold, italic, strike (expanding) or code (closed) over `[from, to)`: on unless already on everywhere. */
export function toggleInline(doc: Doc, from: number, to: number, kind: Exclude<InlineKind, 'link'>): CommandResult {
  if (from >= to) return { inverse: [], caret: from };
  const spans = spansOf(doc);
  let allOn = true;
  for (const s of spans) {
    if (s.end <= from || s.start >= to) continue;
    if (!s.marks.has(kind)) {
      allOn = false;
      break;
    }
  }
  return { inverse: applyMark(doc, kind, allOn ? null : FLAG, from, to), caret: to };
}

export function setLink(doc: Doc, from: number, to: number, url: string | null): CommandResult {
  if (from >= to) return { inverse: [], caret: from };
  return { inverse: applyMark(doc, 'link', url, from, to), caret: to };
}

export function addComment(doc: Doc, from: number, to: number, id: string): CommandResult {
  if (from >= to) return { inverse: [], caret: from };
  return { inverse: applyMark(doc, COMMENT_PREFIX + id, FLAG, from, to), caret: to };
}

export function removeComment(doc: Doc, id: string): CommandResult {
  const kind = COMMENT_PREFIX + id;
  let first = -1;
  let last = -1;
  for (const s of spansOf(doc)) {
    if (!s.marks.has(kind)) continue;
    if (first < 0) first = s.start;
    last = s.end;
  }
  if (first < 0) return { inverse: [], caret: 0 };
  return { inverse: applyMark(doc, kind, null, first, last), caret: first };
}

// ---- tables -------------------------------------------------------------------------------

/** Number of cells a row really has (lines() pads ragged rows). */
function rawCells(row: Line): number {
  let n = 1;
  for (const ch of row.text) if (ch === CELL_SEP) n++;
  return n;
}

function tableOf(ls: Line[], line: Line): Line[] {
  if (!line.kind || !TABLE_KINDS.has(line.kind)) return [];
  let a = line.index;
  while (a > 0 && ls[a - 1].kind && TABLE_KINDS.has(ls[a - 1].kind as string)) a--;
  let b = line.index;
  while (b + 1 < ls.length && ls[b + 1].kind && TABLE_KINDS.has(ls[b + 1].kind as string)) b++;
  return ls.slice(a, b + 1);
}

/** Insert a `rows` x `cols` table before the line containing `at`; the caret lands in the first cell. */
export function insertTable(doc: Doc, at: number, cols: number, rows: number): CommandResult {
  const inv: Inverse[] = [];
  terminate(doc, inv);
  cols = Math.max(1, cols);
  rows = Math.max(1, rows);
  const line = lineAt(lines(doc), at);
  const pos = line.start;
  const row = CELL_SEP.repeat(cols - 1) + '\n';
  inv.push({ op: 'removeIds', ids: insertPlain(doc, pos, row.repeat(rows)) });
  for (let r = 0; r < rows; r++) {
    const end = pos + r * cols + cols - 1;
    doc.web.markRangeClosed(doc.obj, end, end + 1, BLOCK, r === 0 ? 'table_head' : 'table');
  }
  return { inverse: inv, caret: pos };
}

/** Add an empty row after the row containing `pos`. */
export function addRow(doc: Doc, pos: number): CommandResult {
  const line = lineAt(lines(doc), pos);
  if (!line.cells) return { inverse: [], caret: pos };
  const cols = line.cells.length;
  const ids = insertPlain(doc, line.end + 1, CELL_SEP.repeat(cols - 1) + '\n');
  doc.web.markRangeClosed(doc.obj, line.end + cols, line.end + cols + 1, BLOCK, 'table');
  return { inverse: [{ op: 'removeIds', ids }], caret: line.end + 1 };
}

/** Remove the row containing `pos`; a removed head row hands its role and alignment to the next. */
export function removeRow(doc: Doc, pos: number): CommandResult {
  const ls = lines(doc);
  const line = lineAt(ls, pos);
  if (!line.cells) return { inverse: [], caret: pos };
  const next = ls[line.index + 1];
  const inv: Inverse[] = [removeRange(doc, line.start, line.end + 1)];
  if (line.kind === 'table_head' && next?.cells) {
    const end = line.start + (next.end - next.start);
    inv.push(...setTerminatorMarks(doc, end, { kind: 'table_head', lang: null, align: alignValue(line) }));
  }
  terminate(doc, inv);
  return { inverse: inv, caret: Math.min(line.start, doc.web.textLen(doc.obj) - 1) };
}

/** Insert an empty column at index `col` of the table containing `pos`, in every row. */
export function addColumn(doc: Doc, pos: number, col: number): CommandResult {
  const ls = lines(doc);
  const line = lineAt(ls, pos);
  const rows = tableOf(ls, line);
  if (!rows.length) return { inverse: [], caret: pos };
  const inv: Inverse[] = [];
  let caret = pos;
  for (const row of [...rows].reverse()) {
    const n = rawCells(row);
    const starts = row.cellStarts as number[];
    const at = col < n ? starts[col] : row.end;
    const seps = col < n ? 1 : col - n + 1;
    inv.push({ op: 'removeIds', ids: insertPlain(doc, at, CELL_SEP.repeat(seps)) });
    if (row.index === line.index) caret = col < n ? at : at + seps;
    if (row.kind === 'table_head' && row.align) {
      const a = [...row.align];
      while (a.length < col) a.push(null);
      a.splice(col, 0, null);
      inv.push(...applyMark(doc, ALIGN, encodeAlign(a), row.end + seps, row.end + seps + 1));
    }
  }
  return { inverse: inv, caret };
}

/** Remove column `col` (its cells and one separator each) from every row that has it. */
export function removeColumn(doc: Doc, pos: number, col: number): CommandResult {
  const ls = lines(doc);
  const line = lineAt(ls, pos);
  const rows = tableOf(ls, line);
  if (!rows.length) return { inverse: [], caret: pos };
  const inv: Inverse[] = [];
  for (const row of [...rows].reverse()) {
    const n = rawCells(row);
    if (col >= n) continue;
    const starts = row.cellStarts as number[];
    const cellEnd = col + 1 < n ? starts[col + 1] - 1 : row.end;
    let from: number;
    let to: number;
    if (n === 1) [from, to] = [row.start, row.end];
    else if (col === 0) [from, to] = [row.start, starts[1]];
    else [from, to] = [starts[col] - 1, cellEnd];
    if (from < to) inv.push(removeRange(doc, from, to));
    if (row.kind === 'table_head' && row.align && n > 1) {
      const a = [...row.align];
      a.splice(col, 1);
      const end = row.end - (to - from);
      inv.push(...applyMark(doc, ALIGN, a.some((x) => x) ? encodeAlign(a) : null, end, end + 1));
    }
  }
  return { inverse: inv, caret: line.start };
}

/** Column alignment of the table containing `pos` (an `align` mark on the head row). */
export function setAlign(doc: Doc, pos: number, aligns: readonly Align[]): CommandResult {
  const ls = lines(doc);
  const rows = tableOf(ls, lineAt(ls, pos));
  if (!rows.length) return { inverse: [], caret: pos };
  const head = rows[0];
  const value = aligns.some((a) => a) ? encodeAlign(aligns) : null;
  return { inverse: applyMark(doc, ALIGN, value, head.end, head.end + 1), caret: pos };
}

export interface CellTarget extends CommandResult {
  /** End of the target cell (select `[caret, end)`). */
  end: number;
}

function cellIndexAt(row: Line, pos: number): number {
  const starts = row.cellStarts as number[];
  const n = rawCells(row);
  let i = 0;
  while (i + 1 < n && starts[i + 1] <= pos) i++;
  return i;
}

function cellBounds(row: Line, i: number): [number, number] {
  const starts = row.cellStarts as number[];
  const n = rawCells(row);
  return [starts[i], i + 1 < n ? starts[i + 1] - 1 : row.end];
}

/** Tab: the next cell, the next row's first cell, or a new row after the last cell. */
export function nextCell(doc: Doc, pos: number): CellTarget {
  const ls = lines(doc);
  const row = lineAt(ls, pos);
  if (!row.cells) return { inverse: [], caret: pos, end: pos };
  const i = cellIndexAt(row, pos);
  if (i + 1 < rawCells(row)) {
    const [a, b] = cellBounds(row, i + 1);
    return { inverse: [], caret: a, end: b };
  }
  const next = ls[row.index + 1];
  if (next?.cells) {
    const [a, b] = cellBounds(next, 0);
    return { inverse: [], caret: a, end: b };
  }
  const r = addRow(doc, pos);
  return { inverse: r.inverse, caret: r.caret, end: r.caret };
}

/** Shift+Tab: the previous cell, or the previous row's last cell. */
export function prevCell(doc: Doc, pos: number): CellTarget {
  const ls = lines(doc);
  const row = lineAt(ls, pos);
  if (!row.cells) return { inverse: [], caret: pos, end: pos };
  const i = cellIndexAt(row, pos);
  if (i > 0) {
    const [a, b] = cellBounds(row, i - 1);
    return { inverse: [], caret: a, end: b };
  }
  const prev = ls[row.index - 1];
  if (prev?.cells) {
    const [a, b] = cellBounds(prev, rawCells(prev) - 1);
    return { inverse: [], caret: a, end: b };
  }
  return { inverse: [], caret: row.start, end: row.start };
}

// ---- models (paste) -----------------------------------------------------------------------

/** What notes-core's markdown parser returns: text with its line kinds and inline marks. */
export interface NotesModel {
  /** The text; line `i` of `lines` describes the line ending at the i-th "\n". */
  text: string;
  lines: ModelLine[];
  inline: ModelMark[];
}

export interface ModelLine {
  kind: LineKind | null;
  lang?: string;
  align?: Align[];
}

export interface ModelMark {
  /** Code-point offsets into `text`. */
  from: number;
  to: number;
  kind: InlineKind;
  /** The URL for `link`; flags need none. */
  value?: string;
}

/**
 * Insert a model at `at`: its terminators carry their kinds, the host line keeps its own, and
 * the text gets the model's inline marks only (an expanding mark ending at `at` would grow
 * over it, as it does for typing, but a paste brings its own formatting).
 */
export function insertModel(doc: Doc, at: number, model: NotesModel): CommandResult {
  const before = at > 0 ? spanAt(spansOf(doc), at - 1) : null;
  const ids = insertPlain(doc, at, model.text);
  if (!ids.length) return { inverse: [], caret: at };
  const end = at + cpLen(model.text);
  if (before) {
    for (const kind of EXPANDING_KINDS) if (before.marks.has(kind)) doc.web.unmarkRange(doc.obj, at, end, kind);
  }
  let k = 0;
  let i = 0;
  for (const ch of model.text) {
    if (ch === '\n') {
      const line = model.lines[k++];
      const end = at + i;
      if (line?.kind) doc.web.markRangeClosed(doc.obj, end, end + 1, BLOCK, line.kind);
      if (line?.lang) doc.web.markRangeClosed(doc.obj, end, end + 1, LANG, line.lang);
      if (line?.align?.some((a) => a)) doc.web.markRangeClosed(doc.obj, end, end + 1, ALIGN, encodeAlign(line.align));
    }
    i++;
  }
  for (const m of model.inline) markKind(doc, m.kind, m.value ?? FLAG, at + m.from, at + m.to);
  return { inverse: [{ op: 'removeIds', ids }], caret: end };
}

// ---- input rules --------------------------------------------------------------------------

export type InputRule =
  /** Remove `prefix` code points at the line start and set the kind ("# ", "- ", "1. ", "> ", "[ ] "). */
  | { type: 'kind'; kind: LineKind; prefix: number }
  /** Inline code between the backticks at `open` and `close` (offsets within the line). */
  | { type: 'code'; open: number; close: number }
  /** "```lang" + Enter: the line becomes a code line with `lang`. */
  | { type: 'fence'; lang: string }
  /** "```" + Enter on a code line: leave the code block. */
  | { type: 'endFence' }
  /** "|a|b|" + Enter: a table with these head cells and one empty row. */
  | { type: 'table'; cells: string[] };

const KIND_PREFIXES: [string, LineKind][] = [
  ['# ', 'h1'],
  ['## ', 'h2'],
  ['### ', 'h3'],
  ['- [ ] ', 'task'],
  ['- [x] ', 'task_done'],
  ['[ ] ', 'task'],
  ['[x] ', 'task_done'],
  ['- ', 'bullet'],
  ['* ', 'bullet'],
  ['> ', 'quote']
];

/**
 * Pure: given the line's text after the typed character (a space or a backtick), or the line
 * about to receive Enter ("\n"), the conversion to apply, if any. `caret` is the code-point
 * offset within the line after typing.
 */
export function inputRule(lineText: string, caret: number, typed: ' ' | '`' | '\n', kind: LineKind | null): InputRule | null {
  if (typed === ' ') {
    const head = cpSlice(lineText, 0, caret);
    if (kind === 'bullet' && (head === '[ ] ' || head === '[x] ')) {
      return { type: 'kind', kind: head === '[ ] ' ? 'task' : 'task_done', prefix: 4 };
    }
    if (kind !== null) return null;
    for (const [prefix, k] of KIND_PREFIXES) if (head === prefix) return { type: 'kind', kind: k, prefix: cpLen(prefix) };
    if (/^\d{1,2}\. $/.test(head)) return { type: 'kind', kind: 'number', prefix: cpLen(head) };
    return null;
  }
  if (typed === '`') {
    if (kind === 'code') return null;
    const cps = Array.from(lineText);
    const close = caret - 1;
    if (cps[close] !== '`') return null;
    const open = cps.lastIndexOf('`', close - 1);
    if (open < 0 || open === close - 1) return null;
    if (cps.slice(open + 1, close).includes(CELL_SEP)) return null;
    return { type: 'code', open, close };
  }
  // Enter.
  if (kind === 'code') return lineText === '```' ? { type: 'endFence' } : null;
  if (kind !== null) return null;
  const fence = /^```([\w+#.-]*)$/.exec(lineText);
  if (fence) return { type: 'fence', lang: fence[1] };
  const table = /^\|(.*)\|$/.exec(lineText);
  if (table && !lineText.includes(CELL_SEP)) return { type: 'table', cells: table[1].split('|').map((c) => c.trim()) };
  return null;
}

/** Apply a conversion from `inputRule` to `line` (as returned by lines() before applying). */
export function applyInputRule(doc: Doc, line: Line, rule: InputRule): CommandResult {
  const inv: Inverse[] = [];
  switch (rule.type) {
    case 'kind': {
      inv.push(...deleteRange(doc, line.start, line.start + rule.prefix).inverse);
      inv.push(...setLineKind(doc, line.start, line.start, rule.kind).inverse);
      return { inverse: inv, caret: line.start };
    }
    case 'code': {
      const open = line.start + rule.open;
      const close = line.start + rule.close;
      inv.push(...applyMark(doc, 'code', FLAG, open + 1, close));
      inv.push(removeRange(doc, close, close + 1));
      inv.push(removeRange(doc, open, open + 1));
      return { inverse: inv, caret: close - 1 };
    }
    case 'fence': {
      inv.push(...deleteRange(doc, line.start, line.end).inverse);
      inv.push(...applyMark(doc, BLOCK, 'code', line.start, line.start + 1));
      if (rule.lang) inv.push(...applyMark(doc, LANG, rule.lang, line.start, line.start + 1));
      return { inverse: inv, caret: line.start };
    }
    case 'endFence': {
      inv.push(...deleteRange(doc, line.start, line.end).inverse);
      inv.push(...setTerminatorMarks(doc, line.start, { kind: null, lang: null, align: null }));
      return { inverse: inv, caret: line.start };
    }
    case 'table': {
      const cols = Math.max(1, rule.cells.length);
      inv.push(...deleteRange(doc, line.start, line.end).inverse);
      const head = rule.cells.join(CELL_SEP);
      inv.push({ op: 'removeIds', ids: insertPlain(doc, line.start, head) });
      const end = line.start + cpLen(head);
      inv.push(...applyMark(doc, BLOCK, 'table_head', end, end + 1));
      const ids = insertPlain(doc, end + 1, CELL_SEP.repeat(cols - 1) + '\n');
      inv.push({ op: 'removeIds', ids });
      doc.web.markRangeClosed(doc.obj, end + cols, end + cols + 1, BLOCK, 'table');
      return { inverse: inv, caret: end + 1 };
    }
  }
}

export { idsOf };
