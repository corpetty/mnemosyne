// The DOM layer of the notes editor (phase 6, docs/plans/2026-10-12-hashseq-collaboration.md):
// `lines(doc)` rendered into ONE contenteditable root, one child element per line keyed by its
// terminator's element id. The mapping between DOM points and document offsets lives in
// points.ts and is re-exported here. No input handling here (that is the editor's next layer);
// the hooks it needs are `invalidateLine`, `lineElementOf` and the selection helpers.
//
// Ported by name from hashseq's web/kb.js (MIT, David Rusu): extractText (FILLER landing pads
// and the sentinel <br> are presentation, stripped on the way out), offsetOfPoint and
// locateOffset (here locatePoint, with kb's `preferAfter` affinity: at a boundary the caret
// lands outside a styled span unless asked for the other side, and always after a filler),
// setSelectionRangeIn (setSelectionOffsets), renderEditableInto (a ZWSP filler after every
// styled element and before a leading one, a sentinel <br> where a line box would collapse) and
// rerenderBlock's signature-keyed incremental rendering (APP_NOTES.md 14, 17, 18, 20).
//
// Index unit: the model's offsets are Unicode code points; DOM offsets are UTF-16 units. The
// conversions use model.ts's helpers.
//
// Checked in Chromium 152 and WebKitGTK 2.54 (2026-10-22, a dev page driving the Selection):
// both keep a programmatic point where it was put (the end of a <strong>'s text, a filler, an
// empty cell, (tr, i), (root, n)); both type INSIDE a span from the end of its text and OUTSIDE
// from the filler after it (the fillers do their job), and both type OUTSIDE a span from the
// start of its text, so an insertion at a mark's start is plain whatever the affinity; a point
// after all lines maps to the last line's end. Caret stepping (Selection.modify) visits every
// offset in order in both; each filler costs one dead stop in both, and Chromium adds two per
// task line (before and after the checkbox island), WebKit none.
//
// DOM shape produced by `render`:
//   <div class="notes-editor">
//     <div class="ne-line ne-h1" data-line="<id>">text<strong>bold</strong>​</div>
//     <div class="ne-line ne-task"><span class="ne-check" contenteditable="false" …></span>text</div>
//     <div class="ne-line"><br data-sentinel></div>
//     <table class="ne-table"><tbody>
//       <tr class="ne-row ne-table_head" data-line="<id>"><th>a</th><th>b</th></tr>
//       <tr class="ne-row ne-table" data-line="<id>"><td>c</td><td><br data-sentinel></td></tr>
//     </tbody></table>
//   </div>
// Cells are joined by U+001F on extraction (the model's CELL_SEP); every line contributes its
// "\n" (except an unterminated tail, `data-tail`); chrome (`data-chrome`, never editable)
// contributes nothing.
import { parseChip } from '../chips.js';
import { CELL_SEP, TABLE_KINDS, type Align, type Line, type Run } from '../model.js';
import { FILLER, TAIL_KEY, isChrome } from './points.js';

export {
  FILLER,
  TAIL_KEY,
  extractText,
  getSelectionOffsets,
  locatePoint,
  offsetOfPoint,
  setSelectionOffsets,
  type DomPoint,
  type SelectionOffsets
} from './points.js';

export const ROOT_CLASS = 'notes-editor';

/** Called when a task line's checkbox is clicked: the index of the line in the rendered `lines`. */
export type ToggleTaskHandler = (lineIndex: number) => void;

// ---- rendering ------------------------------------------------------------------------------

interface RenderState {
  /** Line id (or TAIL_KEY) -> its element (a line div or a table row). */
  byId: Map<string, HTMLElement>;
  sig: Map<string, string>;
}

const states = new WeakMap<HTMLElement, RenderState>();

const keyOf = (line: Line): string => line.id ?? TAIL_KEY;

function runSig(r: Run): string {
  return [
    r.text,
    (r.bold ? 'b' : '') + (r.italic ? 'i' : '') + (r.strike ? 's' : '') + (r.code ? 'c' : ''),
    r.link ?? '',
    r.linkConflict ? r.linkConflict.join('|') : '',
    r.comments.join(',')
  ].join('\u0001');
}

function lineSig(line: Line): string {
  return JSON.stringify([line.kind, line.lang, line.id === null, line.runs.map(runSig)]);
}

function rowSig(line: Line, aligns: readonly Align[]): string {
  return JSON.stringify([line.kind, line.id === null, aligns, (line.cells ?? []).map((c) => c.map(runSig))]);
}

/** The alignment a table run renders with: the first row that carries one (the head row). */
function tableAligns(rows: readonly Line[]): Align[] {
  const cols = Math.max(1, ...rows.map((r) => r.cells?.length ?? 1));
  const found = rows.find((r) => r.align)?.align ?? [];
  const out: Align[] = [];
  for (let i = 0; i < cols; i++) out.push(found[i] ?? null);
  return out;
}

function isTableLine(line: Line): boolean {
  return line.kind !== null && TABLE_KINDS.has(line.kind) && line.cells !== null;
}

/**
 * Render `lines` into `root`, incrementally: a line whose signature (kind, text, runs, lang,
 * cells, alignment) is unchanged keeps its exact DOM node, so a remote edit elsewhere never
 * disturbs the caret or an IME composition; changed lines are rebuilt, new ones inserted,
 * removed ones dropped. Consecutive table rows share one <table>, reused while any of its rows
 * survives. Classes that depend on neighbours (code block first/last) are toggled in place.
 */
export function render(root: HTMLElement, lines: readonly Line[]): void {
  const doc = root.ownerDocument;
  const prev = states.get(root) ?? { byId: new Map(), sig: new Map() };
  const next: RenderState = { byId: new Map(), sig: new Map() };
  const claimedTables = new Set<HTMLTableElement>();
  const blocks: HTMLElement[] = [];

  const reuseOrBuild = (line: Line, sig: string, build: () => HTMLElement): HTMLElement => {
    const key = keyOf(line);
    const old = prev.byId.get(key);
    let el = old && prev.sig.get(key) === sig && old.isConnected && root.contains(old) ? old : null;
    if (!el) el = build();
    next.byId.set(key, el);
    next.sig.set(key, sig);
    return el;
  };

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!isTableLine(line)) {
      blocks.push(reuseOrBuild(line, lineSig(line), () => buildLine(doc, line)));
      i++;
      continue;
    }
    let j = i;
    while (j < lines.length && isTableLine(lines[j])) j++;
    const rows = lines.slice(i, j);
    const aligns = tableAligns(rows);
    let table: HTMLTableElement | null = null;
    for (const r of rows) {
      const t = prev.byId.get(keyOf(r))?.closest('table');
      if (t && t.parentNode === root && !claimedTables.has(t)) {
        table = t;
        break;
      }
    }
    if (!table) {
      table = doc.createElement('table');
      table.className = 'ne-table';
      table.appendChild(doc.createElement('tbody'));
    }
    claimedTables.add(table);
    let tbody: HTMLElement | null = table.tBodies[0] ?? null;
    if (!tbody) {
      tbody = doc.createElement('tbody');
      table.appendChild(tbody);
    }
    const rowEls = rows.map((r) => reuseOrBuild(r, rowSig(r, aligns), () => buildRow(doc, r, aligns)));
    syncChildren(tbody, rowEls);
    blocks.push(table);
    i = j;
  }
  syncChildren(root, blocks);
  let prevCode = false;
  for (let k = 0; k < root.children.length; k++) {
    const el = root.children[k];
    const code = el.classList.contains('ne-code');
    const nextCode = root.children[k + 1]?.classList.contains('ne-code') ?? false;
    if (code) {
      el.classList.toggle('ne-code-first', !prevCode);
      el.classList.toggle('ne-code-last', !nextCode);
    }
    prevCode = code;
  }
  states.set(root, next);
}

/**
 * Make `parent`'s children exactly `desired`, in order, touching as little as possible: nodes
 * not wanted any more are removed, new ones inserted; a node already in place is left alone.
 */
function syncChildren(parent: Node, desired: readonly Node[]): void {
  const want = new Set(desired);
  let cursor: Node | null = parent.firstChild;
  for (const el of desired) {
    while (cursor && cursor !== el && !want.has(cursor)) {
      const after: Node | null = cursor.nextSibling;
      parent.removeChild(cursor);
      cursor = after;
    }
    if (cursor === el) {
      cursor = cursor.nextSibling;
      continue;
    }
    parent.insertBefore(el, cursor);
  }
  while (cursor) {
    const after: Node | null = cursor.nextSibling;
    parent.removeChild(cursor);
    cursor = after;
  }
}

/** Forget a line's signature so the next `render` rebuilds its element (after a native edit). */
export function invalidateLine(root: HTMLElement, lineId: string | null): void {
  states.get(root)?.sig.delete(lineId ?? TAIL_KEY);
}

/** The element rendered for a line id, if any. */
export function lineElementById(root: HTMLElement, lineId: string | null): HTMLElement | null {
  const el = states.get(root)?.byId.get(lineId ?? TAIL_KEY) ?? null;
  return el && root.contains(el) ? el : null;
}

/** The line element (a `.ne-line` div or a `tr.ne-row`) containing `node`, if any. */
export function lineElementOf(root: HTMLElement, node: Node | null): HTMLElement | null {
  let n: Node | null = node;
  while (n && n !== root) {
    if (n instanceof HTMLElement && n.dataset.line !== undefined) return n;
    n = n.parentNode;
  }
  return null;
}

/** The line id of a line element (null for an unterminated tail). */
export function lineIdOf(el: HTMLElement): string | null {
  const id = el.dataset.line;
  return id === undefined || id === TAIL_KEY ? null : id;
}

function buildLine(doc: Document, line: Line): HTMLElement {
  const el = doc.createElement('div');
  el.className = 'ne-line' + (line.kind ? ` ne-${line.kind}` : '');
  el.dataset.line = keyOf(line);
  if (line.id === null) el.dataset.tail = '1';
  if (line.kind === 'task' || line.kind === 'task_done') el.appendChild(buildCheckbox(doc, line.kind === 'task_done'));
  if (line.kind === 'code' && line.lang) {
    const lang = doc.createElement('span');
    lang.className = 'ne-lang';
    lang.contentEditable = 'false';
    lang.dataset.chrome = '1';
    lang.textContent = line.lang;
    el.appendChild(lang);
  }
  renderInline(doc, el, line.runs);
  return el;
}

function buildCheckbox(doc: Document, done: boolean): HTMLElement {
  const box = doc.createElement('span');
  box.className = 'ne-check';
  box.contentEditable = 'false';
  box.dataset.chrome = '1';
  box.setAttribute('role', 'checkbox');
  box.setAttribute('aria-checked', done ? 'true' : 'false');
  box.setAttribute('tabindex', '-1');
  return box;
}

function buildRow(doc: Document, line: Line, aligns: readonly Align[]): HTMLElement {
  const tr = doc.createElement('tr');
  tr.className = 'ne-row' + (line.kind ? ` ne-${line.kind}` : '');
  tr.dataset.line = keyOf(line);
  if (line.id === null) tr.dataset.tail = '1';
  const cells = line.cells ?? [[]];
  // lines() pads ragged rows to the table's width; the padding cells are not in the text
  // (`data-pad`): extraction emits no separator for them and the caret never maps into them.
  let raw = 1;
  for (const ch of line.text) if (ch === CELL_SEP) raw++;
  cells.forEach((cell, i) => {
    const td = doc.createElement(line.kind === 'table_head' ? 'th' : 'td');
    if (i >= raw) td.dataset.pad = '1';
    const align = aligns[i];
    if (align) td.style.textAlign = align;
    renderInline(doc, td, cell);
    tr.appendChild(td);
  });
  return tr;
}

/**
 * The inline content of a line or cell: one wrapper chain per run (link > comment mark >
 * strong > em > s > code), FILLER pads after every element and before a leading one (kb's
 * rule: Chrome normalises a boundary insert into the previous inline element, so the caret
 * needs a text node to sit in outside the mark), a sentinel <br> when there is no text.
 */
function renderInline(doc: Document, container: HTMLElement, runs: readonly Run[]): void {
  let hasText = false;
  for (const run of runs) {
    if (!run.text) continue;
    hasText = true;
    container.appendChild(buildRun(doc, run));
  }
  if (!hasText) {
    const br = doc.createElement('br');
    br.dataset.sentinel = '1';
    container.appendChild(br);
    return;
  }
  const content = Array.from(container.childNodes).filter((n) => !isChrome(n));
  for (const child of content) {
    if (child.nodeType !== Node.ELEMENT_NODE) continue;
    const sib = child.nextSibling;
    if (!(sib && sib.nodeType === Node.TEXT_NODE && (sib as Text).data.startsWith(FILLER))) {
      child.after(doc.createTextNode(FILLER));
    }
  }
  const first = content[0];
  if (first && first.nodeType === Node.ELEMENT_NODE) first.before(doc.createTextNode(FILLER));
}

const SAFE_HREF = /^(?:https?|mailto):/i;

function buildRun(doc: Document, run: Run): Node {
  let node: Node = doc.createTextNode(run.text);
  const wrap = (tag: string): HTMLElement => {
    const el = doc.createElement(tag);
    el.appendChild(node);
    node = el;
    return el;
  };
  if (run.code) wrap('code');
  if (run.strike) wrap('s');
  if (run.italic) wrap('em');
  if (run.bold) wrap('strong');
  if (run.comments.length) {
    const mark = wrap('mark');
    mark.className = 'ne-comment';
    mark.dataset.comment = run.comments.join(' ');
  }
  if (run.link !== null) {
    const a = wrap('a') as HTMLAnchorElement;
    const chip = parseChip(run.link);
    if (chip) {
      // A chip (phase 11): a pill, never a hyperlink. The text stays ordinary editable text.
      a.className = `ne-chip ne-chip-${chip.kind}`;
      a.dataset.chip = run.link;
    } else {
      // A peer wrote this URL (C12): only these schemes may be followed, also in a read-only
      // editor, where a `javascript:` link would otherwise run on click.
      if (SAFE_HREF.test(run.link)) a.href = run.link;
      a.title = run.link;
    }
  } else if (run.linkConflict) {
    const span = wrap('span');
    span.className = 'ne-link-conflict';
    span.title = 'Link conflict';
    span.dataset.links = run.linkConflict.join(' ');
  }
  return node;
}
