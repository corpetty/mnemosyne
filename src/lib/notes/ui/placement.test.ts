import { describe, expect, it } from 'vitest';
import { leafRects, placeToolbar, type Rect } from './placement.js';

const r = (left: number, top: number, right: number, bottom: number): Rect => ({ left, top, right, bottom });
// A box 500 wide and 400 tall at (100, 50) on the page; a toolbar 200 x 30.
const box = r(100, 50, 600, 450);
const toolbar = { width: 200, height: 30 };

describe('placeToolbar', () => {
  it('goes above the first line, centred on it, relative to the box', () => {
    // Selection on one line at x 300..400, y 200..220.
    const p = placeToolbar({ rects: [r(300, 200, 400, 220)], box, toolbar });
    expect(p).toEqual({ side: 'above', top: 200 - 8 - 30 - 50, left: 350 - 100 - 100 });
  });

  it('hangs from the first line of a selection over several lines, not the last', () => {
    const p = placeToolbar({
      rects: [r(300, 200, 590, 220), r(120, 220, 590, 240), r(120, 240, 200, 260)],
      box,
      toolbar
    });
    expect(p?.side).toBe('above');
    expect(p?.top).toBe(200 - 8 - 30 - 50);
    expect(p?.left).toBe(445 - 100 - 100); // centred on the first line (300..590)
  });

  it('goes below the last line when there is no room above', () => {
    // The first line is 20px under the box's top: 8 + 30 do not fit.
    const p = placeToolbar({ rects: [r(300, 70, 400, 90), r(120, 90, 300, 110)], box, toolbar });
    expect(p?.side).toBe('below');
    expect(p?.top).toBe(110 + 8 - 50);
    expect(p?.left).toBe(210 - 100 - 100); // centred on the last line (120..300)
  });

  it('fits above exactly when the toolbar and gap reach the box top', () => {
    expect(placeToolbar({ rects: [r(300, 88, 400, 108)], box, toolbar })?.side).toBe('above'); // 88-38 = 50
    expect(placeToolbar({ rects: [r(300, 87, 400, 107)], box, toolbar })?.side).toBe('below');
  });

  it('clamps at the left edge', () => {
    const p = placeToolbar({ rects: [r(105, 200, 125, 220)], box, toolbar });
    expect(p?.left).toBe(4); // box.left + margin, relative to the box
  });

  it('clamps at the right edge', () => {
    const p = placeToolbar({ rects: [r(570, 200, 596, 220)], box, toolbar });
    expect(p?.left).toBe(500 - 4 - 200);
  });

  it('pins to the left margin when the box is narrower than the toolbar', () => {
    const narrow = r(100, 50, 250, 450);
    expect(placeToolbar({ rects: [r(150, 200, 200, 220)], box: narrow, toolbar })?.left).toBe(4);
  });

  it('adds the box scroll offsets (the toolbar scrolls with the text)', () => {
    const plain = placeToolbar({ rects: [r(300, 200, 400, 220)], box, toolbar })!;
    const scrolled = placeToolbar({
      rects: [r(300, 200, 400, 220)],
      box,
      toolbar,
      scrollTop: 120,
      scrollLeft: 7
    })!;
    expect(scrolled.top).toBe(plain.top + 120);
    expect(scrolled.left).toBe(plain.left + 7);
    expect(scrolled.side).toBe(plain.side);
  });

  it('uses the visible area: a first line scrolled out above puts the toolbar below the last line', () => {
    // First line is above the box's top (scrolled away), the selection ends at y 120.
    const p = placeToolbar({ rects: [r(300, 20, 400, 40), r(120, 100, 300, 120)], box, toolbar });
    expect(p?.side).toBe('below');
    expect(p?.top).toBe(128 - 50);
  });

  it('stays inside the box when the selection fills the view', () => {
    const p = placeToolbar({ rects: [r(120, 60, 590, 200), r(120, 200, 590, 445)], box, toolbar });
    expect(p?.side).toBe('below');
    expect(p?.top).toBe(450 - 30 - 50); // box.bottom - height, relative to the box
  });

  it('is null with no rects, empty rects, or a selection scrolled out of view', () => {
    expect(placeToolbar({ rects: [], box, toolbar })).toBeNull();
    expect(placeToolbar({ rects: [r(300, 200, 300, 200)], box, toolbar })).toBeNull();
    expect(placeToolbar({ rects: [r(300, 10, 400, 30)], box, toolbar })).toBeNull();
    expect(placeToolbar({ rects: [r(300, 500, 400, 520)], box, toolbar })).toBeNull();
  });
});

describe('leafRects', () => {
  it('drops the boxes of whole lines that contain text runs', () => {
    const line = r(100, 200, 600, 220);
    const text = r(120, 200, 300, 220);
    const out = leafRects([line, text, r(100, 220, 600, 240), r(100, 220, 250, 240)]);
    expect(out).toEqual([text, r(100, 220, 250, 240)]);
  });

  it('drops zero-width rects (a selected line break) but keeps them when nothing else is left', () => {
    expect(leafRects([r(120, 200, 300, 220), r(600, 200, 600, 220)])).toEqual([r(120, 200, 300, 220)]);
    expect(leafRects([r(600, 200, 600, 220)])).toEqual([r(600, 200, 600, 220)]);
  });

  it('keeps duplicates and rects with no height out', () => {
    expect(leafRects([r(10, 10, 50, 30), r(10, 10, 50, 30), r(10, 30, 50, 30)])).toEqual([
      r(10, 10, 50, 30),
      r(10, 10, 50, 30)
    ]);
  });
});
