import { describe, expect, it } from 'vitest';
import { pair, single, type Client } from '../test-harness.js';
import {
  addColumn,
  addComment,
  addRow,
  deleteRange,
  insertTable,
  insertText,
  setAlign,
  setLineKind,
  setLink,
  splitLine,
  toggleInline
} from '../commands.js';
import { CELL_SEP, LINE_KINDS, cpLen, ensureTerminated, lines, type LineKind } from '../model.js';
import { removeRange } from '../ops.js';
import {
  FILLER,
  extractText,
  getSelectionOffsets,
  lineElementOf,
  locatePoint,
  offsetOfPoint,
  render,
  setSelectionOffsets
} from './dom.js';

function mount(): HTMLElement {
  const root = document.createElement('div');
  root.tabIndex = 0; // jsdom focuses a contenteditable only with a tabindex; real engines do not need it
  root.contentEditable = 'true';
  document.body.appendChild(root);
  return root;
}

/** A doc with these lines (kind after a colon, e.g. "Title:h1"). */
function docOf(...spec: string[]): Client {
  const A = single();
  ensureTerminated(A.doc);
  let at = 0;
  for (const s of spec) {
    const m = /^(.*?)(?::([a-z_0-9]+))?$/s.exec(s) as RegExpExecArray;
    const [, text, kind] = m;
    insertText(A.doc, at, text);
    splitLine(A.doc, at + cpLen(text));
    if (kind) setLineKind(A.doc, at, at, kind as LineKind);
    at += cpLen(text) + 1;
  }
  deleteRange(A.doc, at - 1, at);
  return A;
}

function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const ALPHABET = ['a', 'b', 'c', ' ', 'x', 'é', '😀', '👩‍💻', '日', '\n', 'z', 'q', '-', '|'];

/** A random document built with the commands: lines of every kind, marks, tables, emoji. */
function randomDoc(seed: number, steps = 25): Client {
  const rnd = mulberry32(seed);
  const pick = <T,>(xs: readonly T[]): T => xs[Math.floor(rnd() * xs.length)];
  const A = single();
  ensureTerminated(A.doc);
  const len = () => A.web.textLen(A.doc.obj);
  for (let i = 0; i < steps; i++) {
    const n = len();
    const at = Math.floor(rnd() * n);
    const r = rnd();
    if (r < 0.35) {
      let text = '';
      const k = 1 + Math.floor(rnd() * 6);
      for (let j = 0; j < k; j++) text += pick(ALPHABET);
      insertText(A.doc, at, text);
    } else if (r < 0.45) {
      splitLine(A.doc, at);
    } else if (r < 0.6) {
      setLineKind(A.doc, at, at, rnd() < 0.15 ? null : pick(LINE_KINDS));
    } else if (r < 0.72) {
      const to = Math.min(n - 1, at + 1 + Math.floor(rnd() * 5));
      toggleInline(A.doc, at, to, pick(['bold', 'italic', 'strike', 'code'] as const));
    } else if (r < 0.78) {
      const to = Math.min(n - 1, at + 1 + Math.floor(rnd() * 5));
      setLink(A.doc, at, to, 'https://example.com/' + i);
    } else if (r < 0.83) {
      const to = Math.min(n - 1, at + 1 + Math.floor(rnd() * 5));
      addComment(A.doc, at, to, 'c' + i);
    } else if (r < 0.9) {
      insertTable(A.doc, at, 1 + Math.floor(rnd() * 3), 1 + Math.floor(rnd() * 3));
    } else if (r < 0.95) {
      if (rnd() < 0.5) addRow(A.doc, at);
      else addColumn(A.doc, at, Math.floor(rnd() * 3));
    } else if (n > 2) {
      deleteRange(A.doc, at, Math.min(n - 1, at + 1 + Math.floor(rnd() * 4)));
    }
    ensureTerminated(A.doc);
  }
  return A;
}

describe('render -> extractText', () => {
  it('round-trips random documents', () => {
    for (let seed = 1; seed <= 40; seed++) {
      const A = randomDoc(seed);
      const root = mount();
      render(root, lines(A.doc));
      expect(extractText(root), `seed ${seed}`).toBe(A.text());
      root.remove();
    }
  });

  it('round-trips tables with ragged rows, emoji and marks', () => {
    const A = docOf('before', 'after');
    insertTable(A.doc, 7, 2, 2);
    const ls = lines(A.doc);
    insertText(A.doc, ls[1].start, 'h😀');
    insertText(A.doc, ls[1].start + 3, 'x');
    // Ragged: a third cell on the second row only.
    const row2 = lines(A.doc)[2];
    insertText(A.doc, row2.end, CELL_SEP + 'extra');
    toggleInline(A.doc, ls[1].start, ls[1].start + 2, 'bold');
    const root = mount();
    render(root, lines(A.doc));
    expect(extractText(root)).toBe(A.text());
    expect(root.querySelectorAll('table').length).toBe(1);
    expect(root.querySelectorAll('tr').length).toBe(2);
    expect(root.querySelectorAll('tr')[0].children.length).toBe(3); // padded to the widest row
    expect(root.querySelectorAll('th').length).toBe(3);
    expect(root.querySelectorAll('th[data-pad]').length).toBe(1);
    expect(root.querySelectorAll('td').length).toBe(3);
    expect(root.querySelector('th strong')?.textContent).toBe('h😀');
  });

  it('renders every kind, keeping chrome out of the text', () => {
    const A = docOf('Title:h1', 'Sub:h2', 'Small:h3', 'b:bullet', 'one:number', 'two:number', 'todo:task', 'done:task_done', 'q:quote', 'code:code', '', 'plain');
    A.web.markRangeClosed(A.doc.obj, lines(A.doc)[9].end, lines(A.doc)[9].end + 1, 'lang', 'rust');
    const root = mount();
    render(root, lines(A.doc));
    expect(extractText(root)).toBe(A.text());
    const els = Array.from(root.children) as HTMLElement[];
    expect(els.map((e) => e.className)).toEqual([
      'ne-line ne-h1',
      'ne-line ne-h2',
      'ne-line ne-h3',
      'ne-line ne-bullet',
      'ne-line ne-number',
      'ne-line ne-number',
      'ne-line ne-task',
      'ne-line ne-task_done',
      'ne-line ne-quote',
      'ne-line ne-code ne-code-first ne-code-last',
      'ne-line',
      'ne-line'
    ]);
    const box = els[6].querySelector('.ne-check') as HTMLElement;
    expect(box.getAttribute('role')).toBe('checkbox');
    expect(box.getAttribute('aria-checked')).toBe('false');
    expect(box.contentEditable).toBe('false');
    expect((els[7].querySelector('.ne-check') as HTMLElement).getAttribute('aria-checked')).toBe('true');
    const lang = els[9].querySelector('.ne-lang') as HTMLElement;
    expect(lang.textContent).toBe('rust');
    expect(lang.contentEditable).toBe('false');
    expect(els[10].innerHTML).toBe('<br data-sentinel="1">');
    expect(els[10].dataset.line).toBe(lines(A.doc)[10].id);
  });

  it('joins consecutive code lines and marks the first and last', () => {
    const A = docOf('a:code', 'b:code', 'c:code', 'plain', 'd:code');
    const root = mount();
    render(root, lines(A.doc));
    const cls = Array.from(root.children).map((e) => e.className);
    expect(cls[0]).toBe('ne-line ne-code ne-code-first');
    expect(cls[1]).toBe('ne-line ne-code');
    expect(cls[2]).toBe('ne-line ne-code ne-code-last');
    expect(cls[4]).toBe('ne-line ne-code ne-code-first ne-code-last');
    // The plain line between is where the number counter would reset too.
    setLineKind(A.doc, lines(A.doc)[3].start, lines(A.doc)[3].start, 'code');
    render(root, lines(A.doc));
    expect(Array.from(root.children).map((e) => e.className)).toEqual([
      'ne-line ne-code ne-code-first',
      'ne-line ne-code',
      'ne-line ne-code',
      'ne-line ne-code',
      'ne-line ne-code ne-code-last'
    ]);
  });

  it('wraps inline runs, with fillers after styled spans and before a leading one', () => {
    const A = docOf('ab cd ef');
    toggleInline(A.doc, 0, 2, 'bold');
    toggleInline(A.doc, 3, 5, 'code');
    toggleInline(A.doc, 3, 5, 'italic');
    setLink(A.doc, 6, 8, 'https://x.y/');
    const root = mount();
    render(root, lines(A.doc));
    expect(root.firstElementChild?.innerHTML).toBe(
      `${FILLER}<strong>ab</strong>${FILLER} <em><code>cd</code></em>${FILLER} <a href="https://x.y/" title="https://x.y/">ef</a>${FILLER}`
    );
    expect(extractText(root)).toBe('ab cd ef\n');
  });

  it('renders comment marks and link conflicts', () => {
    const { relay, A, B } = pair();
    ensureTerminated(A.doc);
    insertText(A.doc, 0, 'see here');
    relay.settle();
    setLink(A.doc, 4, 8, 'https://a/');
    setLink(B.doc, 4, 8, 'https://b/');
    relay.settle();
    addComment(A.doc, 0, 3, 'c1');
    const root = mount();
    render(root, lines(A.doc));
    const mark = root.querySelector('mark.ne-comment') as HTMLElement;
    expect(mark.dataset.comment).toBe('c1');
    expect(mark.textContent).toBe('see');
    const conflict = root.querySelector('.ne-link-conflict') as HTMLElement;
    expect(conflict.title).toBe('Link conflict');
    expect(conflict.textContent).toBe('here');
    expect(root.querySelector('a')).toBeNull();
    expect(extractText(root)).toBe('see here\n');
  });

  it('shows table alignment and pads ragged rows with empty cells', () => {
    const A = docOf('x');
    insertTable(A.doc, 0, 3, 2);
    setAlign(A.doc, 0, ['left', 'center', 'right']);
    const root = mount();
    render(root, lines(A.doc));
    const cells = Array.from(root.querySelectorAll('tr')[1].children) as HTMLElement[];
    expect(cells.map((c) => c.style.textAlign)).toEqual(['left', 'center', 'right']);
    expect(cells.map((c) => c.innerHTML)).toEqual(Array(3).fill('<br data-sentinel="1">'));
    expect(extractText(root)).toBe(A.text());
  });

  it('extracts an unterminated tail and what a browser may leave behind', () => {
    const A = single();
    A.web.textInsert(A.doc.obj, 0, 'a\nb');
    const root = mount();
    render(root, lines(A.doc));
    expect(extractText(root)).toBe('a\nb');
    expect((root.lastElementChild as HTMLElement).dataset.tail).toBe('1');
    // A native edit: a <br> inside a line is a newline, stray root text joins the next line,
    // a chrome element contributes nothing even when text sits around it.
    root.innerHTML = 'stray<div data-line="p">x<br>y</div><div data-line="q"><span data-chrome="1">[]</span>z</div>';
    expect(extractText(root)).toBe('strayx\ny\nz\n');
  });
});

describe('incremental render', () => {
  it('keeps the DOM node of every unchanged line after a remote edit elsewhere', () => {
    const { relay, A, B } = pair();
    ensureTerminated(A.doc);
    insertText(A.doc, 0, 'one\ntwo\nthree');
    setLineKind(A.doc, 4, 4, 'bullet');
    insertTable(A.doc, 8, 2, 2);
    relay.settle();
    const root = mount();
    render(root, lines(A.doc));
    const before = Array.from(root.querySelectorAll('[data-line]'));
    const table = root.querySelector('table');
    expect(before.length).toBe(5);
    // B edits the first line and adds a table row.
    insertText(B.doc, 0, 'remote ');
    addRow(B.doc, lines(B.doc)[2].start);
    relay.settle();
    render(root, lines(A.doc));
    const after = Array.from(root.querySelectorAll('[data-line]'));
    expect(after.length).toBe(6);
    expect(after[0]).not.toBe(before[0]); // rebuilt
    expect(after[1]).toBe(before[1]);
    expect(after[2]).toBe(before[2]);
    expect(after[4]).toBe(before[3]); // the new row sits at after[3]
    expect(after[5]).toBe(before[4]);
    expect(root.querySelector('table')).toBe(table);
    expect(extractText(root)).toBe(A.text());
  });

  it('drops removed lines and inserts new ones in place', () => {
    const A = docOf('a', 'b', 'c');
    const root = mount();
    render(root, lines(A.doc));
    const [ea, eb, ec] = Array.from(root.children);
    deleteRange(A.doc, 2, 4); // "b\n"
    splitLine(A.doc, 0);
    render(root, lines(A.doc));
    const now = Array.from(root.children);
    expect(now.length).toBe(3);
    expect(now[1]).toBe(ea);
    expect(now[2]).toBe(ec);
    expect(now).not.toContain(eb);
    expect(extractText(root)).toBe('\na\nc\n');
  });

  it('rebuilds a line whose marks changed but keeps its id', () => {
    const A = docOf('hello');
    const root = mount();
    render(root, lines(A.doc));
    const el = root.firstElementChild as HTMLElement;
    toggleInline(A.doc, 0, 2, 'bold');
    render(root, lines(A.doc));
    expect(root.firstElementChild).not.toBe(el);
    expect((root.firstElementChild as HTMLElement).dataset.line).toBe(el.dataset.line);
  });

  it('keeps the table element when its first row goes away', () => {
    const A = docOf('x');
    insertTable(A.doc, 0, 2, 3);
    const root = mount();
    render(root, lines(A.doc));
    const table = root.querySelector('table');
    const rows = Array.from(root.querySelectorAll('tr'));
    const head = lines(A.doc)[0];
    removeRange(A.doc, head.start, head.end + 1); // the raw op: the next row keeps its own kind
    render(root, lines(A.doc));
    expect(root.querySelector('table')).toBe(table);
    expect(Array.from(root.querySelectorAll('tr'))).toEqual([rows[1], rows[2]]);
    expect(extractText(root)).toBe(A.text());
  });
});

describe('offset mapping', () => {
  /** Every caret position: 0 .. the end of the last line (the doc's length, after the final "\n", is none). */
  function roundTrip(A: Client, root: HTMLElement, label: string): void {
    const n = A.web.textLen(A.doc.obj);
    for (let off = 0; off < n; off++) {
      for (const after of [false, true]) {
        const p = locatePoint(root, off, after);
        expect(root.contains(p.node), `${label} off ${off} after ${after}: node in root`).toBe(true);
        expect(offsetOfPoint(root, p.node, p.offset), `${label} off ${off} after ${after}`).toBe(off);
      }
    }
    const past = locatePoint(root, n);
    expect(offsetOfPoint(root, past.node, past.offset), `${label} past the end`).toBe(n - 1);
    expect(offsetOfPoint(root, root, root.childNodes.length), `${label} end of root`).toBe(n - 1);
  }

  it('round-trips every offset of random documents, both affinities', () => {
    for (let seed = 1; seed <= 25; seed++) {
      const A = randomDoc(seed);
      const root = mount();
      render(root, lines(A.doc));
      roundTrip(A, root, `seed ${seed}`);
      root.remove();
    }
  });

  it('lands in text nodes after fillers and chooses the side at a mark boundary', () => {
    const A = docOf('ab cd');
    toggleInline(A.doc, 0, 2, 'bold');
    toggleInline(A.doc, 3, 5, 'code');
    const root = mount();
    render(root, lines(A.doc));
    const line = root.firstElementChild as HTMLElement;
    // FILLER <strong>ab</strong> FILLER " " <code>cd</code> FILLER
    const leadFiller = line.childNodes[0] as Text;
    const strong = line.childNodes[1] as HTMLElement;
    const midFiller = line.childNodes[2] as Text;
    const space = line.childNodes[3] as Text;
    const code = line.childNodes[4] as HTMLElement;
    const tailFiller = line.childNodes[5] as Text;
    expect(locatePoint(root, 0)).toEqual({ node: leadFiller, offset: 1 }); // after the filler, outside <strong>
    expect(locatePoint(root, 0, true)).toEqual({ node: strong.firstChild, offset: 0 }); // inside
    expect(locatePoint(root, 2)).toEqual({ node: strong.firstChild, offset: 2 }); // inside at its end
    expect(locatePoint(root, 2, true)).toEqual({ node: space, offset: 0 }); // outside: past the filler, into what follows
    expect(locatePoint(root, 3)).toEqual({ node: space, offset: 1 });
    expect(locatePoint(root, 3, true)).toEqual({ node: code.firstChild, offset: 0 });
    expect(locatePoint(root, 5)).toEqual({ node: code.firstChild, offset: 2 });
    expect(locatePoint(root, 5, true)).toEqual({ node: line, offset: 6 }); // line end, after the tail filler
    expect(offsetOfPoint(root, tailFiller, 0)).toBe(5);
    expect(offsetOfPoint(root, tailFiller, 1)).toBe(5);
    expect(offsetOfPoint(root, line, 0)).toBe(0);
    expect(offsetOfPoint(root, root, 1)).toBe(5); // after the only line = its end
    // Past the end lands at the end of the last line.
    expect(locatePoint(root, 99)).toEqual({ node: line, offset: 6 });
  });

  it('handles empty lines, emoji and task chrome', () => {
    const A = docOf('', 'x😀y:task', '');
    const root = mount();
    render(root, lines(A.doc));
    const [l0, l1, l2] = Array.from(root.children) as HTMLElement[];
    expect(locatePoint(root, 0)).toEqual({ node: l0, offset: 0 }); // before the sentinel <br>
    expect(locatePoint(root, 0, true)).toEqual({ node: l0, offset: 0 });
    const text = l1.childNodes[1] as Text; // after the checkbox
    expect(locatePoint(root, 1)).toEqual({ node: text, offset: 0 });
    expect(locatePoint(root, 2)).toEqual({ node: text, offset: 1 });
    expect(locatePoint(root, 3)).toEqual({ node: text, offset: 3 }); // past the surrogate pair
    expect(locatePoint(root, 4)).toEqual({ node: text, offset: 4 });
    expect(locatePoint(root, 4, true)).toEqual({ node: l1, offset: 2 });
    expect(locatePoint(root, 5)).toEqual({ node: l2, offset: 0 });
    expect(offsetOfPoint(root, text, 2)).toBe(3); // mid-surrogate rounds up, like cpIndex
    expect(offsetOfPoint(root, l1, 0)).toBe(1); // before the checkbox = line start
    expect(offsetOfPoint(root, l1, 1)).toBe(1);
    expect(offsetOfPoint(root, l1.firstChild as Node, 0)).toBe(1); // inside the chrome
    expect(offsetOfPoint(root, document.body, 0)).toBeNull();
  });

  it('maps table cells: separators between cells, row ends, padded cells', () => {
    const A = docOf('x');
    insertTable(A.doc, 0, 2, 2);
    insertText(A.doc, 0, 'ab');
    insertText(A.doc, 3, 'c');
    // text: "ab\u001fc\n\u001f\nx\n"
    const root = mount();
    render(root, lines(A.doc));
    const [r0, r1] = Array.from(root.querySelectorAll('tr'));
    const [c00, c01] = Array.from(r0.children);
    const [c10, c11] = Array.from(r1.children);
    expect(locatePoint(root, 2)).toEqual({ node: c00.firstChild, offset: 2 });
    expect(locatePoint(root, 2, true)).toEqual({ node: c00, offset: 1 }); // still in the cell
    expect(locatePoint(root, 3)).toEqual({ node: c01.firstChild, offset: 0 });
    expect(locatePoint(root, 4)).toEqual({ node: c01.firstChild, offset: 1 });
    expect(locatePoint(root, 4, true)).toEqual({ node: c01, offset: 1 });
    expect(locatePoint(root, 5)).toEqual({ node: c10, offset: 0 }); // empty cell, before its <br>
    expect(locatePoint(root, 6)).toEqual({ node: c11, offset: 0 });
    expect(locatePoint(root, 7).node).toBe(root.children[1].firstChild); // "x"
    expect(offsetOfPoint(root, r0, 0)).toBe(0);
    expect(offsetOfPoint(root, r0, 1)).toBe(3);
    expect(offsetOfPoint(root, r0, 2)).toBe(4);
    expect(offsetOfPoint(root, r1, 1)).toBe(6);
    expect(offsetOfPoint(root, root.querySelector('tbody') as Node, 1)).toBe(5);
    expect(offsetOfPoint(root, root, 1)).toBe(7);
    expect(offsetOfPoint(root, root, 2)).toBe(8); // after the last line: its end
    expect(offsetOfPoint(root, root.querySelector('table') as Node, 0)).toBe(0);
  });
});

describe('selection helpers', () => {
  it('set and get offsets, keeping the direction', () => {
    const A = docOf('hello', 'wörld 😀');
    toggleInline(A.doc, 2, 4, 'italic');
    const root = mount();
    render(root, lines(A.doc));
    root.focus();
    setSelectionOffsets(root, 1, 9);
    expect(getSelectionOffsets(root)).toEqual({ anchor: 1, focus: 9 });
    setSelectionOffsets(root, 12, 7);
    expect(getSelectionOffsets(root)).toEqual({ anchor: 12, focus: 7 });
    setSelectionOffsets(root, 4, 4, true);
    expect(getSelectionOffsets(root)).toEqual({ anchor: 4, focus: 4 });
    const sel = document.getSelection() as Selection;
    expect(lineElementOf(root, sel.anchorNode)).toBe(root.firstElementChild);
    sel.removeAllRanges();
    expect(getSelectionOffsets(root)).toBeNull();
  });
});
