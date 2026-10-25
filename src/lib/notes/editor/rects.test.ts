// jsdom has no layout: Range.getClientRects and Element.getBoundingClientRect are stubbed with a
// fixed 10 px per character grid (a collapsed range measures nothing, as in WebKit).
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { caretRect, rangeRects } from './rects.js';

const rect = (left: number, top: number, width: number, height: number) =>
  ({ left, top, right: left + width, bottom: top + height, width, height, x: left, y: top }) as DOMRect;
const list = (...rs: DOMRect[]) => Object.assign(rs, { item: (i: number) => rs[i] }) as unknown as DOMRectList;

let root: HTMLElement;
const protoRange = Range.prototype as unknown as { getClientRects?: () => DOMRectList };
const protoEl = Element.prototype as unknown as { getBoundingClientRect: () => DOMRect };
let savedRange: unknown;
let savedEl: unknown;

beforeEach(() => {
  savedRange = protoRange.getClientRects;
  savedEl = protoEl.getBoundingClientRect;
  protoRange.getClientRects = function (this: Range) {
    const { startContainer: n, startOffset: s, endOffset: e, collapsed } = this;
    if (collapsed || n.nodeType !== Node.TEXT_NODE || n !== this.endContainer) return list();
    const line = Array.from(root.children).indexOf(n.parentElement as Element);
    return list(rect(100 + s * 10, 50 + line * 20, (e - s) * 10, 20));
  };
  protoEl.getBoundingClientRect = function (this: Element) {
    const line = Array.from(root.children).indexOf(this);
    return line < 0 ? rect(100, 50, 400, 100) : rect(100, 50 + line * 20, 400, 20);
  };
  root = document.createElement('div');
  root.innerHTML = '<div data-line="a">abc</div><div data-line="b"><br data-sentinel></div><div data-line="c">xy</div>';
  document.body.append(root);
});

afterEach(() => {
  protoRange.getClientRects = savedRange as never;
  protoEl.getBoundingClientRect = savedEl as never;
  root.remove();
});

const origin = { left: 90, top: 40 };

describe('caretRect', () => {
  it('falls back to the previous character right edge when a collapsed range measures nothing', () => {
    // offset 3 = end of "abc": the right edge of "c"
    expect(caretRect(root, 3, origin)).toEqual({ x: 40, y: 10, w: 0, h: 20 });
  });
  it('is inside the text mid-line', () => {
    expect(caretRect(root, 1, origin)).toEqual({ x: 20, y: 10, w: 0, h: 20 });
  });
  it('is the start of the line at offset 0 of a later line', () => {
    // "abc\n" = 4, the empty line "\n" = 1: "xy" starts at 5; no previous char in its text node
    expect(caretRect(root, 5, origin)).toEqual({ x: 10, y: 50, w: 0, h: 20 });
  });
  it('gives an empty line the left edge and height of its box', () => {
    expect(caretRect(root, 4, origin)).toEqual({ x: 10, y: 30, w: 0, h: 20 });
  });
});

describe('rangeRects', () => {
  it('measures the characters in the range, relative to the origin', () => {
    expect(rangeRects(root, 1, 3, origin)).toEqual([{ x: 20, y: 10, w: 20, h: 20 }]);
  });
  it('takes either order and is empty when collapsed', () => {
    expect(rangeRects(root, 3, 1, origin)).toEqual(rangeRects(root, 1, 3, origin));
    expect(rangeRects(root, 2, 2, origin)).toEqual([]);
  });
});
