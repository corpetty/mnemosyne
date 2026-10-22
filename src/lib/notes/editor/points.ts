// DOM points <-> document offsets for the notes editor (the other half of dom.ts, which
// renders): text extraction, offsetOfPoint / locatePoint and the selection helpers, over the
// DOM shape dom.ts documents. Ported by name from hashseq's web/kb.js (MIT, David Rusu):
// extractText, offsetOfPoint, locateOffset (locatePoint here, with its `preferAfter`
// affinity) and setSelectionRangeIn (setSelectionOffsets). Offsets are code points; DOM text
// offsets are UTF-16 (model.ts's helpers convert).
import { CELL_SEP, cpLen } from '../model.js';

/** Zero-width space: DOM-only caret landing pads, never content. */
export const FILLER = '\u200b';
/** The `data-line` key of an unterminated last line (the doc is normally always terminated). */
export const TAIL_KEY = '$tail';

export interface DomPoint {
  node: Node;
  /** UTF-16 offset in a text node, child index in an element. */
  offset: number;
}

// ---- the rendered shape ---------------------------------------------------------------------

export function isChrome(n: Node): boolean {
  return n.nodeType === Node.ELEMENT_NODE && (n as HTMLElement).dataset.chrome !== undefined;
}

function isSentinel(n: Node): boolean {
  return n.nodeType === Node.ELEMENT_NODE && (n as HTMLElement).tagName === 'BR' && (n as HTMLElement).dataset.sentinel !== undefined;
}

const BLOCK_TAGS = new Set(['DIV', 'P', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'PRE', 'BLOCKQUOTE', 'LI', 'UL', 'OL', 'SECTION']);

function isLineEl(n: Node): n is HTMLElement {
  if (n.nodeType !== Node.ELEMENT_NODE) return false;
  const el = n as HTMLElement;
  return el.dataset.line !== undefined || BLOCK_TAGS.has(el.tagName);
}

const isTable = (n: Node): n is HTMLTableElement => n.nodeType === Node.ELEMENT_NODE && (n as Element).tagName === 'TABLE';
const isRow = (n: Node): n is HTMLTableRowElement => n.nodeType === Node.ELEMENT_NODE && (n as Element).tagName === 'TR';
const isCell = (n: Node): n is HTMLTableCellElement =>
  n.nodeType === Node.ELEMENT_NODE && ((n as Element).tagName === 'TD' || (n as Element).tagName === 'TH');
const isPad = (n: Node): boolean => (n as HTMLElement).dataset.pad !== undefined;
const isSection = (n: Node): boolean =>
  n.nodeType === Node.ELEMENT_NODE && ['TBODY', 'THEAD', 'TFOOT'].includes((n as Element).tagName);

/**
 * One walk over the rendered structure that serves both extraction (`stop` null: the whole
 * text) and offsetOfPoint (`stop` set: the text before that DOM point). Robust to what a
 * browser does to a contenteditable: stray text at the root or in a table joins the next
 * line, a <div> the engine made counts as a line, a non-sentinel <br> is a newline.
 */
class Extractor {
  out = '';
  done = false;

  /** The last line element: a point past its content maps to its end, never past the doc. */
  private lastLine: Element | null;

  constructor(
    root: HTMLElement,
    private stop: DomPoint | null
  ) {
    this.lastLine = stop ? lastLineElement(root) : null;
  }

  private at(node: Node, index: number): boolean {
    if (this.stop && this.stop.node === node && this.stop.offset === index) this.done = true;
    return this.done;
  }

  root(root: Node): void {
    const kids = root.childNodes;
    for (let i = 0; i < kids.length && !this.at(root, i); i++) {
      const child = kids[i];
      if (isTable(child)) this.table(child);
      else if (isLineEl(child)) this.line(child);
      else this.inline(child);
    }
    this.at(root, kids.length);
  }

  private line(el: HTMLElement): void {
    this.inlineChildren(el);
    this.terminate(el);
  }

  /** The line's "\n", unless it is an unterminated tail or the mapped point lies beyond the last line. */
  private terminate(el: HTMLElement): void {
    if (this.done) return;
    if (el === this.lastLine) {
      this.done = true;
      return;
    }
    if (el.dataset.tail === undefined) this.out += '\n';
  }

  private table(table: HTMLElement): void {
    const kids = table.childNodes;
    for (let i = 0; i < kids.length && !this.at(table, i); i++) {
      const child = kids[i];
      if (isSection(child)) {
        const rows = child.childNodes;
        for (let k = 0; k < rows.length && !this.at(child, k); k++) this.rowOrStray(rows[k]);
        this.at(child, rows.length);
      } else this.rowOrStray(child);
    }
    this.at(table, kids.length);
  }

  private rowOrStray(n: Node): void {
    if (isRow(n)) this.row(n);
    else if (isLineEl(n)) this.line(n);
    else this.inline(n);
  }

  private row(tr: HTMLElement): void {
    // The point "before cell i" (tr, i) is the start of that cell: after the separator.
    const kids = tr.childNodes;
    let cells = 0;
    for (let i = 0; i < kids.length; i++) {
      const child = kids[i];
      if (isCell(child)) {
        if (isPad(child)) {
          // Not in the text: a point inside it is the row end.
          if (this.at(tr, i) || (this.stop && child.contains(this.stop.node))) this.done = true;
          if (this.done) return;
          continue;
        }
        if (cells > 0) this.out += CELL_SEP;
        cells++;
        if (this.at(tr, i)) return;
        this.inlineChildren(child);
      } else {
        if (this.at(tr, i)) return;
        this.inline(child);
      }
      if (this.done) return;
    }
    if (this.at(tr, kids.length)) return;
    this.terminate(tr);
  }

  private inlineChildren(el: Node): void {
    const kids = el.childNodes;
    for (let i = 0; i < kids.length && !this.at(el, i); i++) this.inline(kids[i]);
    this.at(el, kids.length);
  }

  private inline(n: Node): void {
    if (this.done) return;
    if (n.nodeType === Node.TEXT_NODE) {
      const data = (n as Text).data;
      if (this.stop && this.stop.node === n) {
        this.out += data.slice(0, this.stop.offset).replaceAll(FILLER, '');
        this.done = true;
      } else this.out += data.replaceAll(FILLER, '');
      return;
    }
    if (n.nodeType !== Node.ELEMENT_NODE) return;
    if (isChrome(n)) {
      if (this.stop && n.contains(this.stop.node)) this.done = true;
      return;
    }
    if ((n as Element).tagName === 'BR') {
      if (!isSentinel(n)) this.out += '\n';
      return;
    }
    if (isTable(n)) this.table(n as HTMLElement);
    else this.inlineChildren(n);
  }
}

/**
 * The document text the DOM shows: lines joined by "\n" (and ending with one), cells joined by
 * U+001F, fillers, sentinels and chrome stripped. Equals the model's text for anything
 * `render` produced.
 */
export function extractText(root: HTMLElement): string {
  const x = new Extractor(root, null);
  x.root(root);
  return x.out;
}

/**
 * The code-point offset of a DOM point (text offsets are UTF-16), or null outside `root`. A
 * point past the last line's content (the engine puts one there on a click below the text) is
 * that line's end: the doc's own length, after the final "\n", is never a caret position.
 */
export function offsetOfPoint(root: HTMLElement, node: Node, offset: number): number | null {
  if (!root.contains(node)) return null;
  const x = new Extractor(root, { node, offset });
  x.root(root);
  return cpLen(x.out);
}

/** The last line element (a div or a table row), if any. */
function lastLineElement(root: HTMLElement): Element | null {
  const last = root.lastElementChild;
  if (last && isTable(last)) {
    const rows = last.querySelectorAll('tr');
    return rows[rows.length - 1] ?? last;
  }
  return last;
}

/**
 * Locate code-point offset `target` as a DOM point. The caret lands in a text node when one is
 * there, after any filler. At a boundary between a text node and a styled element, the default
 * keeps the caret in the text node ending there (outside the element that follows, inside one
 * that ends there); `preferAfter` resolves past it, into whatever starts there and outside
 * whatever ends there. Line and cell ends are always inside their own element, never the next.
 */
export function locatePoint(root: HTMLElement, target: number, preferAfter = false): DomPoint {
  const w = new Locator(Math.max(0, target), preferAfter);
  return w.root(root) ?? endOfLast(root);
}

/** Past the end: the end of the last line (or cell), else the root itself. */
function endOfLast(root: HTMLElement): DomPoint {
  let last: Element | null = root.lastElementChild;
  if (last && isTable(last)) {
    const rows = last.querySelectorAll('tr');
    const row = rows[rows.length - 1] ?? null;
    const cells = row ? Array.from(row.children).filter((c) => isCell(c) && !isPad(c)) : [];
    last = cells[cells.length - 1] ?? row ?? last;
  }
  return endOf(last ?? root);
}

class Locator {
  constructor(
    private remaining: number,
    private preferAfter: boolean
  ) {}

  root(root: Node): DomPoint | null {
    for (const child of Array.from(root.childNodes)) {
      let hit: DomPoint | null;
      if (isTable(child)) hit = this.table(child);
      else if (isLineEl(child)) hit = this.line(child);
      else hit = this.inline(child);
      if (hit) return hit;
    }
    return null;
  }

  private line(el: HTMLElement): DomPoint | null {
    const hit = this.inlineChildren(el);
    if (hit) return hit;
    if (this.remaining === 0) return endOf(el);
    if (el.dataset.tail === undefined) this.remaining--;
    return null;
  }

  private table(table: HTMLElement): DomPoint | null {
    for (const child of Array.from(table.childNodes)) {
      if (isSection(child)) {
        for (const row of Array.from(child.childNodes)) {
          const hit = this.rowOrStray(row);
          if (hit) return hit;
        }
      } else {
        const hit = this.rowOrStray(child);
        if (hit) return hit;
      }
    }
    return null;
  }

  private rowOrStray(n: Node): DomPoint | null {
    if (isRow(n)) return this.row(n);
    if (isLineEl(n)) return this.line(n);
    return this.inline(n);
  }

  private row(tr: HTMLElement): DomPoint | null {
    let last: HTMLElement | null = null;
    for (const child of Array.from(tr.childNodes)) {
      if (isCell(child)) {
        if (isPad(child)) continue;
        if (last) {
          if (this.remaining === 0) return endOf(last);
          this.remaining--;
        }
        last = child;
        const hit = this.inlineChildren(child);
        if (hit) return hit;
      } else {
        const hit = this.inline(child);
        if (hit) return hit;
      }
    }
    if (this.remaining === 0) return last ? endOf(last) : endOf(tr);
    if (tr.dataset.tail === undefined) this.remaining--;
    return null;
  }

  private inlineChildren(el: Node): DomPoint | null {
    for (const child of Array.from(el.childNodes)) {
      const hit = this.inline(child);
      if (hit) return hit;
    }
    return null;
  }

  private inline(n: Node): DomPoint | null {
    if (n.nodeType === Node.TEXT_NODE) {
      const data = (n as Text).data;
      const clean = cpLen(data.replaceAll(FILLER, ''));
      if (this.remaining < clean || (!this.preferAfter && this.remaining === clean)) {
        // Clean code points -> raw UTF-16, stepping past fillers so the caret lands after
        // them (off the element boundary).
        let raw = 0;
        let seen = 0;
        while (raw < data.length && (seen < this.remaining || data[raw] === FILLER)) {
          if (data[raw] === FILLER) {
            raw++;
            continue;
          }
          raw += (data.codePointAt(raw) ?? 0) > 0xffff ? 2 : 1;
          seen++;
        }
        return { node: n, offset: raw };
      }
      this.remaining -= clean;
      return null;
    }
    if (n.nodeType !== Node.ELEMENT_NODE || isChrome(n)) return null;
    if ((n as Element).tagName === 'BR') {
      if (isSentinel(n)) return null;
      if (this.remaining === 0) return { node: n.parentNode as Node, offset: indexIn(n) };
      this.remaining--;
      return null;
    }
    if (isTable(n)) return this.table(n as HTMLElement);
    return this.inlineChildren(n);
  }
}

function indexIn(n: Node): number {
  return Array.prototype.indexOf.call((n.parentNode as Node).childNodes, n);
}

/** The caret position at the end of a line, cell or root: before a sentinel <br>, else after everything. */
function endOf(el: Node): DomPoint {
  const last = el.lastChild;
  if (last && isSentinel(last)) return { node: el, offset: indexIn(last) };
  return { node: el, offset: el.childNodes.length };
}

// ---- selection -----------------------------------------------------------------------------

export interface SelectionOffsets {
  anchor: number;
  focus: number;
}

/** The selection as code-point offsets, or null when it is not inside `root`. */
export function getSelectionOffsets(root: HTMLElement): SelectionOffsets | null {
  const sel = root.ownerDocument.getSelection();
  if (!sel || sel.rangeCount === 0 || !sel.anchorNode || !sel.focusNode) return null;
  const anchor = offsetOfPoint(root, sel.anchorNode, sel.anchorOffset);
  const focus = offsetOfPoint(root, sel.focusNode, sel.focusOffset);
  if (anchor === null || focus === null) return null;
  return { anchor, focus };
}

/** Place the selection at code-point offsets (direction kept); see `locatePoint` for `preferAfter`. */
export function setSelectionOffsets(root: HTMLElement, anchor: number, focus = anchor, preferAfter = false): void {
  const sel = root.ownerDocument.getSelection();
  if (!sel) return;
  const a = locatePoint(root, anchor, preferAfter);
  const f = anchor === focus ? a : locatePoint(root, focus, preferAfter);
  if (typeof sel.setBaseAndExtent === 'function') {
    sel.setBaseAndExtent(a.node, a.offset, f.node, f.offset);
    return;
  }
  const range = root.ownerDocument.createRange();
  range.setStart(a.node, a.offset);
  range.collapse(true);
  sel.removeAllRanges();
  sel.addRange(range);
  if (a !== f) sel.extend(f.node, f.offset);
}
