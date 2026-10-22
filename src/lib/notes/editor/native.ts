// The native-typing half of the input layer: what the browser did to one line, as a diff
// against the model line, and the mark affinity its placement of the typed text implies.
// Ported by name from hashseq's web/kb.js (MIT, David Rusu): applyDiffAt (the caret
// disambiguates a prefix/suffix diff that borders equal text, APP_NOTES 17) and
// reconcileMarkAffinity (the browser's rendering of a boundary keystroke is the intent,
// APP_NOTES 18). Offsets are code points.
import { CELL_SEP, COMMENT_PREFIX, FLAG, cpLen, spansOf, valuesAt, type Doc } from '../model.js';
import { applyMark, type Inverse } from '../ops.js';
import { FILLER, isChrome, type DomPoint } from './points.js';

export type Diff =
  | { kind: 'insert'; p: number; text: string }
  | { kind: 'remove'; p: number; n: number }
  | { kind: 'replace'; p: number; n: number; text: string };

/**
 * The edit that turns `prev` into `next`, located by `caret` (the caret after the edit, in
 * code points of `next`) when that story checks out: typed text ends at the caret, deleted
 * text started there. Otherwise the common prefix/suffix diff. Null when equal.
 */
export function diffAt(prev: string, next: string, caret: number | null): Diff | null {
  if (prev === next) return null;
  const P = Array.from(prev);
  const N = Array.from(next);
  const same = (ai: number, bi: number, n: number): boolean => {
    for (let i = 0; i < n; i++) if (P[ai + i] !== N[bi + i]) return false;
    return true;
  };
  if (caret !== null) {
    if (N.length > P.length) {
      const n = N.length - P.length;
      const p = caret - n;
      if (p >= 0 && p <= P.length && same(0, 0, p) && same(p, p + n, P.length - p)) {
        return { kind: 'insert', p, text: N.slice(p, p + n).join('') };
      }
    } else if (N.length < P.length) {
      const n = P.length - N.length;
      if (caret >= 0 && caret + n <= P.length && same(0, 0, caret) && same(caret + n, caret, N.length - caret)) {
        return { kind: 'remove', p: caret, n };
      }
    }
  }
  let a = 0;
  while (a < P.length && a < N.length && P[a] === N[a]) a++;
  let b = 0;
  while (b < P.length - a && b < N.length - a && P[P.length - 1 - b] === N[N.length - 1 - b]) b++;
  const n = P.length - a - b;
  const text = N.slice(a, N.length - b).join('');
  if (n === 0) return { kind: 'insert', p: a, text };
  if (!text) return { kind: 'remove', p: a, n };
  return { kind: 'replace', p: a, n, text };
}

export interface LineContent {
  text: string;
  /** Code-point offset of the stop point within `text`, or null when it is not in the line. */
  caret: number | null;
}

const isCell = (n: Node): n is HTMLTableCellElement =>
  n.nodeType === Node.ELEMENT_NODE && ((n as Element).tagName === 'TD' || (n as Element).tagName === 'TH');

/**
 * The text one line element (a line div or a table row) shows after a native edit: fillers,
 * chrome and the sentinel stripped, cells joined by CELL_SEP, a non-sentinel <br> a newline
 * except a trailing one (the placeholder a browser leaves in an emptied block), plus where
 * `stop` (the caret) falls in it.
 */
export function lineContent(el: HTMLElement, stop: DomPoint | null = null): LineContent {
  let out = '';
  let caret: number | null = null;
  const mark = () => {
    if (caret === null) caret = cpLen(out);
  };
  const walk = (n: Node): void => {
    if (n.nodeType === Node.TEXT_NODE) {
      const data = (n as Text).data;
      if (stop && stop.node === n) {
        out += data.slice(0, stop.offset).replaceAll(FILLER, '');
        mark();
        out += data.slice(stop.offset).replaceAll(FILLER, '');
      } else out += data.replaceAll(FILLER, '');
      return;
    }
    if (n.nodeType !== Node.ELEMENT_NODE) return;
    const e = n as HTMLElement;
    if (isChrome(e)) {
      if (stop && e.contains(stop.node)) mark();
      return;
    }
    if (e.tagName === 'BR') {
      if (e.dataset.sentinel === undefined) out += '\n';
      return;
    }
    const kids = e.childNodes;
    let cells = 0;
    for (let i = 0; i < kids.length; i++) {
      if (stop && stop.node === e && stop.offset === i) mark();
      const k = kids[i];
      if (e.tagName === 'TR' && isCell(k)) {
        if (k.dataset.pad !== undefined) {
          if (stop && k.contains(stop.node)) mark();
          continue;
        }
        if (cells++ > 0) out += CELL_SEP;
      }
      walk(k);
    }
    if (stop && stop.node === e && stop.offset === kids.length) mark();
  };
  walk(el);
  if (out.endsWith('\n')) out = out.slice(0, -1);
  const len = cpLen(out);
  if (caret !== null && caret > len) caret = len;
  return { text: out, caret };
}

export interface DomFlags {
  bold: boolean;
  italic: boolean;
  strike: boolean;
  code: boolean;
  link: string | null;
  comments: string[];
}

/** The inline styling the DOM gives a point: the wrapper elements between it and its line. */
export function domFlagsAt(node: Node, lineEl: HTMLElement): DomFlags {
  const f: DomFlags = { bold: false, italic: false, strike: false, code: false, link: null, comments: [] };
  let n: Node | null = node.nodeType === Node.TEXT_NODE ? node.parentNode : node;
  while (n && n !== lineEl && n.nodeType === Node.ELEMENT_NODE) {
    const el = n as HTMLElement;
    switch (el.tagName) {
      case 'STRONG':
      case 'B':
        f.bold = true;
        break;
      case 'EM':
      case 'I':
        f.italic = true;
        break;
      case 'S':
      case 'STRIKE':
      case 'DEL':
        f.strike = true;
        break;
      case 'CODE':
        f.code = true;
        break;
      case 'A':
        f.link = el.getAttribute('href');
        break;
      case 'MARK':
        if (el.dataset.comment) f.comments.push(...el.dataset.comment.split(' ').filter(Boolean));
        break;
    }
    n = n.parentNode;
  }
  return f;
}

/**
 * After a native insert of `n` code points at `p`: make the model's marks on them agree with
 * where the browser drew them. Inside a span the model lacks: the mark extends over the
 * contiguous run around the text (closed for code, links and comments, expanding for the
 * rest, as `applyMark` does per kind); outside a span the model gave them: unmarked.
 */
export function reconcileAffinity(doc: Doc, p: number, n: number, dom: DomFlags): Inverse[] {
  const inv: Inverse[] = [];
  const len = doc.web.textLen(doc.obj);
  const extent = (has: (i: number) => boolean): [number, number] => {
    let s = p;
    while (s > 0 && has(s - 1)) s--;
    let e = p + n;
    while (e < len && has(e)) e++;
    return [s, e];
  };
  for (const kind of ['bold', 'italic', 'strike', 'code'] as const) {
    const spans = spansOf(doc);
    const has = (i: number) => valuesAt(spans, i, kind).length > 0;
    if (dom[kind] && !has(p)) {
      const [a, b] = extent(has);
      inv.push(...applyMark(doc, kind, FLAG, a, b));
    } else if (!dom[kind] && has(p)) inv.push(...applyMark(doc, kind, null, p, p + n));
  }
  {
    const spans = spansOf(doc);
    const model = valuesAt(spans, p, 'link');
    if (dom.link && !model.includes(dom.link)) {
      const url = dom.link;
      const [a, b] = extent((i) => valuesAt(spans, i, 'link').includes(url));
      inv.push(...applyMark(doc, 'link', url, a, b));
    } else if (!dom.link && model.length) inv.push(...applyMark(doc, 'link', null, p, p + n));
  }
  {
    const spans = spansOf(doc);
    const model = new Set<string>();
    const at = spans.find((s) => p >= s.start && p < s.end);
    for (const k of at?.marks.keys() ?? []) if (k.startsWith(COMMENT_PREFIX)) model.add(k.slice(COMMENT_PREFIX.length));
    for (const id of dom.comments) {
      if (model.has(id)) continue;
      const kind = COMMENT_PREFIX + id;
      const [a, b] = extent((i) => valuesAt(spans, i, kind).length > 0);
      inv.push(...applyMark(doc, kind, FLAG, a, b));
    }
    for (const id of model) if (!dom.comments.includes(id)) inv.push(...applyMark(doc, COMMENT_PREFIX + id, null, p, p + n));
  }
  return inv;
}
