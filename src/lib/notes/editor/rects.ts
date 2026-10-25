// Where a model offset or range is on screen, for other people's carets (phase 11, task 14):
// client rects from the DOM point `locatePoint` gives (points.ts), turned into coordinates
// relative to an origin (the overlay layer's top-left corner, which scrolls with the editor box).
// A collapsed range has no client rect in WebKit and at a filler or line end in any engine, so a
// caret falls back to the right edge of the character before it, else the left edge of the one
// after, else the (empty) line's content box.
import { locatePoint, type DomPoint } from './points.js';

/** A rectangle relative to the origin. A caret has width 0. */
export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** The origin: the viewport position (client coordinates) the result is measured from. */
export interface Origin {
  left: number;
  top: number;
}

interface Box {
  left: number;
  right: number;
  top: number;
  height: number;
}

const usable = (r: Box | undefined): r is Box => !!r && r.height > 0;

function lastRect(range: Range): Box | undefined {
  const list = range.getClientRects();
  for (let i = list.length - 1; i >= 0; i--) if (usable(list[i])) return list[i];
  return undefined;
}

function firstRect(range: Range): Box | undefined {
  const list = range.getClientRects();
  for (let i = 0; i < list.length; i++) if (usable(list[i])) return list[i];
  return undefined;
}

/** The caret's box at a DOM point as {x: left edge, top, height}, or null when nothing is measurable. */
function boxAt(doc: Document, p: DomPoint): { x: number; top: number; height: number } | null {
  const range = doc.createRange();
  const at = (r: Box | undefined, side: 'left' | 'right') => (r ? { x: r[side], top: r.top, height: r.height } : null);
  if (p.node.nodeType === Node.TEXT_NODE) {
    const text = p.node as Text;
    range.setStart(text, p.offset);
    range.collapse(true);
    const direct = firstRect(range);
    if (direct) return at(direct, 'left');
    // Collapsed ranges measure nothing in WebKit or at a line end: use a neighbour character.
    if (p.offset > 0) {
      range.setStart(text, p.offset - 1);
      range.setEnd(text, p.offset);
      const before = lastRect(range);
      if (before) return at(before, 'right');
    }
    if (p.offset < text.length) {
      range.setStart(text, p.offset);
      range.setEnd(text, p.offset + 1);
      const after = firstRect(range);
      if (after) return at(after, 'left');
    }
    return elementBox(doc, text.parentElement);
  }
  const el = p.node as Element;
  const kids = el.childNodes;
  if (p.offset > 0) {
    const prev = kids[p.offset - 1];
    if (prev.nodeType === Node.TEXT_NODE && (prev as Text).length > 0) {
      const hit = boxAt(doc, { node: prev, offset: (prev as Text).length });
      if (hit) return hit;
    } else if (prev.nodeType === Node.ELEMENT_NODE) {
      range.selectNode(prev);
      const before = lastRect(range);
      if (before) return at(before, 'right');
    }
  }
  if (p.offset < kids.length) {
    const next = kids[p.offset];
    if (next.nodeType === Node.TEXT_NODE && (next as Text).length > 0) {
      const hit = boxAt(doc, { node: next, offset: 0 });
      if (hit) return hit;
    } else if (next.nodeType === Node.ELEMENT_NODE) {
      range.selectNode(next);
      const after = firstRect(range);
      if (after) return at(after, 'left');
    }
  }
  return elementBox(doc, el);
}

/** An empty line or cell: its content box's left edge and full height. */
function elementBox(doc: Document, el: Element | null): { x: number; top: number; height: number } | null {
  if (!el) return null;
  const r = el.getBoundingClientRect();
  if (!(r.height > 0)) return null;
  const css = doc.defaultView?.getComputedStyle(el);
  const left = r.left + (parseFloat(css?.borderLeftWidth ?? '0') || 0) + (parseFloat(css?.paddingLeft ?? '0') || 0);
  return { x: left, top: r.top, height: r.height };
}

/** The caret at code-point `offset` of the document `root` renders, or null when it cannot be measured. */
export function caretRect(root: HTMLElement, offset: number, origin: Origin): Rect | null {
  const box = boxAt(root.ownerDocument, locatePoint(root, offset));
  return box ? { x: box.x - origin.left, y: box.top - origin.top, w: 0, h: box.height } : null;
}

/**
 * The rectangles covering the model range [from, to), one per line fragment, in reading order.
 * Slivers (zero-width rects: fillers, line breaks) are left out; a range with nothing visible gives [].
 */
export function rangeRects(root: HTMLElement, from: number, to: number, origin: Origin): Rect[] {
  if (to < from) [from, to] = [to, from];
  if (from === to) return [];
  const doc = root.ownerDocument;
  const a = locatePoint(root, from, true);
  const b = locatePoint(root, to, false);
  const range = doc.createRange();
  try {
    range.setStart(a.node, a.offset);
    range.setEnd(b.node, b.offset);
  } catch {
    return [];
  }
  if (range.collapsed) return [];
  const out: Rect[] = [];
  const list = range.getClientRects();
  for (let i = 0; i < list.length; i++) {
    const r = list[i];
    if (!(r.width >= 1) || !(r.height > 0)) continue;
    out.push({ x: r.left - origin.left, y: r.top - origin.top, w: r.width, h: r.height });
  }
  return out;
}
