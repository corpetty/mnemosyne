// Where the selection toolbar goes (phase 11, task 3): pure geometry. Inputs are client rects
// (viewport coordinates, as `Range.getClientRects` and `getBoundingClientRect` give them); the
// answer is in the editor box's own coordinate space, ready for `position: absolute` inside it
// (the box scrolls: its content moves under the viewport, so `scrollTop`/`scrollLeft` are added).

export interface Rect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export interface PlacementInput {
  /** The selection's client rects, in document order (one per line box or inline run). */
  rects: Rect[];
  /**
   * The box's visible area in client coordinates (inside its border): the toolbar stays within
   * it, so a box that scrolls or clips never hides the toolbar.
   */
  box: Rect;
  /** The box's scroll offsets (0 when it does not scroll). */
  scrollTop?: number;
  scrollLeft?: number;
  toolbar: { width: number; height: number };
  /** Space between the selection and the toolbar (default 8). */
  gap?: number;
  /** Space kept to the box's left and right edges (default 4). */
  margin?: number;
}

export interface Placement {
  /** Offsets inside the box (absolute positioning), scroll included. */
  top: number;
  left: number;
  side: 'above' | 'below';
}

/**
 * Above the selection's first line when it fits inside the box, else below its last line. Centred
 * on the line it hangs from and clamped inside the box horizontally and vertically. null when
 * there is nothing to attach to: no rects, or the selection is scrolled out of the box's view.
 */
export function placeToolbar(input: PlacementInput): Placement | null {
  const { box, toolbar } = input;
  const gap = input.gap ?? 8;
  const margin = input.margin ?? 4;
  const rects = input.rects.filter((r) => r.bottom > r.top && r.right >= r.left);
  if (!rects.length) return null;
  const first = rects[0];
  const last = rects[rects.length - 1];
  // Entirely above or below the visible part of the box: nothing to point at.
  if (last.bottom < box.top || first.top > box.bottom) return null;

  const fitsAbove = first.top - gap - toolbar.height >= box.top;
  const side = fitsAbove ? 'above' : 'below';
  const anchor = fitsAbove ? first : last;
  let y = fitsAbove ? first.top - gap - toolbar.height : last.bottom + gap;
  // A selection that fills the view leaves no room on either side: keep it inside, on top of the text.
  y = Math.min(y, box.bottom - toolbar.height);
  y = Math.max(y, box.top);

  const lo = box.left + margin;
  const hi = box.right - margin - toolbar.width;
  let x = (anchor.left + anchor.right) / 2 - toolbar.width / 2;
  x = hi < lo ? lo : Math.min(Math.max(x, lo), hi);

  return {
    top: y - box.top + (input.scrollTop ?? 0),
    left: x - box.left + (input.scrollLeft ?? 0),
    side
  };
}

/**
 * The selection's text-run rects: `Range.getClientRects` also returns the boxes of elements the
 * range covers whole (a line's div, a table cell), which contain the text rects. Those are dropped
 * (as are rects with no width, such as a selected line break), so the toolbar centres on the text.
 * Keeps document order; when nothing has a width the zero-width ones are kept.
 */
export function leafRects(rects: Rect[]): Rect[] {
  const real = rects.filter((r) => r.bottom > r.top);
  const wide = real.filter((r) => r.right - r.left >= 1);
  const pool = wide.length ? wide : real;
  const contains = (a: Rect, b: Rect) =>
    a !== b &&
    a.left <= b.left &&
    a.top <= b.top &&
    a.right >= b.right &&
    a.bottom >= b.bottom &&
    // Equal rects (the same run twice) are not containers of each other.
    (a.left < b.left || a.top < b.top || a.right > b.right || a.bottom > b.bottom);
  return pool.filter((r) => !pool.some((o) => contains(r, o)));
}
