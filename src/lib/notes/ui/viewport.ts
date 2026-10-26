// The on-screen keyboard on touch screens: the visual viewport shrinks above it, so what is pinned
// "to the bottom" (the touch bar, the comment sheet) follows the visual viewport's bottom edge, and
// the line being typed is scrolled to just above the bar. Pure parts are exported for the tests.

/** Is the primary pointer coarse (a touch screen)? False where matchMedia is missing (jsdom). */
export function isCoarse(): boolean {
  return typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches;
}

/** Calls `fn` with `isCoarse()` now and whenever it changes; returns the unsubscribe. */
export function watchCoarse(fn: (coarse: boolean) => void): () => void {
  const mq = typeof matchMedia === 'function' ? matchMedia('(pointer: coarse)') : null;
  const on = () => fn(!!mq?.matches);
  on();
  mq?.addEventListener?.('change', on);
  return () => mq?.removeEventListener?.('change', on);
}

/**
 * The visual viewport's bottom as an inset from the layout viewport's bottom (the keyboard's
 * height when the layout viewport keeps its size, iOS), now and on every change; returns the
 * unsubscribe. Without a visualViewport the inset is 0.
 */
export function followKeyboard(fn: (inset: number) => void): () => void {
  const vv = typeof window !== 'undefined' ? window.visualViewport : null;
  const follow = () => fn(vv ? Math.max(0, window.innerHeight - (vv.offsetTop + vv.height)) : 0);
  follow();
  vv?.addEventListener('resize', follow);
  vv?.addEventListener('scroll', follow);
  return () => {
    vv?.removeEventListener('resize', follow);
    vv?.removeEventListener('scroll', follow);
  };
}

/** What `caretScroll` needs, all in client (layout viewport) coordinates. */
export interface CaretScrollInput {
  /** The caret's top and bottom. */
  top: number;
  bottom: number;
  /** The visual viewport: `visualViewport.offsetTop` and `.height`. */
  viewTop: number;
  viewHeight: number;
  /** Height of what is pinned at the visible bottom (the touch bar); 0 when nothing is. */
  bar: number;
  /** Room kept between the caret and the bar (and the visible top). */
  margin?: number;
}

/**
 * How far to scroll (positive = down) so the caret sits just above the bar: 0 while it is
 * between the visible top and `viewTop + viewHeight - bar - margin`. Below that line it is
 * brought up to it; above the visible top it is brought down to `viewTop + margin`.
 */
export function caretScroll({ top, bottom, viewTop, viewHeight, bar, margin = 12 }: CaretScrollInput): number {
  const limit = viewTop + viewHeight - bar - margin;
  if (bottom > limit) {
    const down = bottom - limit;
    // A caret taller than the room left: keep its top visible rather than its bottom.
    return Math.min(down, Math.max(0, top - (viewTop + margin)));
  }
  if (top < viewTop) return top - (viewTop + margin);
  return 0;
}

/** The element that scrolls `el` vertically: the nearest overflowing ancestor, else the page. */
export function scrollParent(el: Element | null): Element | null {
  for (let p = el?.parentElement ?? null; p; p = p.parentElement) {
    if (p === document.body || p === document.documentElement) break;
    const oy = getComputedStyle(p).overflowY;
    if ((oy === 'auto' || oy === 'scroll') && p.scrollHeight > p.clientHeight) return p;
  }
  return document.scrollingElement;
}
