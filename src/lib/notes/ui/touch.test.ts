// The editor on a touch screen in jsdom (stubbed matchMedia and visualViewport): the touch bar is
// the toolbar, its Comment works after the tap took the editor's focus and selection, the comment
// sheet posts through the host, and the caret-visibility scroll (pure).
import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { CommentSelection } from '../comments.js';
import { insertText } from '../commands.js';
import { getSelectionOffsets, setSelectionOffsets } from '../editor/dom.js';
import { ensureTerminated, openDoc } from '../model.js';
import { ORIGIN, WasmHashWeb } from '../test-harness.js';
import NotesEditor from './NotesEditor.svelte';
import TouchBar from './TouchBar.svelte';
import type { NotesHost } from './types.js';
import { caretScroll } from './viewport.js';

globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver;

let target: HTMLElement;
let component: ReturnType<typeof mount> | null = null;
const realMatchMedia = window.matchMedia;

afterEach(() => {
  if (component) unmount(component);
  component = null;
  target?.remove();
  window.matchMedia = realMatchMedia;
  delete (window as unknown as Record<string, unknown>).visualViewport;
});

function pointer(coarse: boolean) {
  const mql = Object.assign(new EventTarget(), { matches: coarse });
  window.matchMedia = (() => mql) as unknown as typeof window.matchMedia;
  const vv = Object.assign(new EventTarget(), { height: innerHeight, offsetTop: 0 });
  Object.defineProperty(window, 'visualViewport', { value: vv, configurable: true });
}

function setup(opts: { coarse: boolean; readOnly?: boolean; post?: boolean }) {
  pointer(opts.coarse);
  const doc = openDoc(new WasmHashWeb(), ORIGIN);
  ensureTerminated(doc);
  insertText(doc, 0, 'We ship the beta on the 3rd.');
  const posted: { sel: CommentSelection; text: string }[] = [];
  const composed: CommentSelection[] = [];
  const said: string[] = [];
  const host: NotesHost = {
    doc,
    readOnly: opts.readOnly ?? false,
    canComment: true,
    undoButtons: true,
    openComments: new Set(),
    localChange() {},
    setTyping() {},
    flush() {},
    bindRemote: () => () => {},
    compose: (sel) => composed.push(sel),
    onMark() {},
    say: (m) => said.push(m),
    ...(opts.post === false
      ? {}
      : {
          postComment: async (sel: CommentSelection, text: string) => {
            posted.push({ sel, text });
          }
        })
  };
  target = document.body.appendChild(document.createElement('div'));
  component = mount(NotesEditor, { target, props: { host, testid: 'ed', label: 'Notes' } });
  flushSync();
  const root = target.querySelector<HTMLElement>('[data-testid="ed"]')!;
  root.tabIndex = 0; // jsdom focuses a contenteditable only with a tabindex
  return { doc, root, posted, composed, said };
}

/** Select [a, b) in the editor as a person would (focus, selection, the selectionchange event). */
function select(root: HTMLElement, a: number, b: number) {
  root.focus();
  setSelectionOffsets(root, a, b);
  document.dispatchEvent(new Event('selectionchange'));
  flushSync();
}

const bar = () => target.querySelector<HTMLElement>('[data-testid="touch-bar"]');
const barButton = (name: string) => bar()!.querySelector<HTMLButtonElement>(`button[aria-label="${name}"]`)!;
const sheet = () => target.querySelector<HTMLElement>('[data-testid="comment-sheet"]');

describe('the editor on a touch screen', () => {
  it('has no slim toolbar and no hint line; the touch bar shows while editing', () => {
    const { root } = setup({ coarse: true });
    expect(target.querySelector('[role="toolbar"][aria-label="Format notes"]')).toBeNull();
    expect(target.textContent).not.toContain('Type / for');
    select(root, 0, 0);
    expect(bar()).not.toBeNull();
  });

  it('keeps the slim toolbar and the hint line on a fine pointer', () => {
    setup({ coarse: false });
    expect(target.querySelector('[role="toolbar"][aria-label="Format notes"]')).not.toBeNull();
    expect(target.textContent).toContain('Type / for');
  });

  it('comments on the remembered selection after the tap took focus and selection', async () => {
    const { root, posted, composed } = setup({ coarse: true });
    select(root, 12, 16); // "beta"
    const comment = barButton('Comment on the selection');
    // What a browser may do on a touch tap: focus moves to the button, the selection goes.
    comment.focus();
    document.getSelection()!.removeAllRanges();
    flushSync();
    expect(bar()).not.toBeNull(); // focus in the bar does not hide it
    comment.click();
    flushSync();
    await tick();
    expect(composed).toEqual([]);
    expect(sheet()).not.toBeNull();
    expect(sheet()!.querySelector('[data-testid="comment-sheet-quote"]')!.textContent!.trim()).toBe('beta');
    const box = sheet()!.querySelector('textarea')!;
    expect(document.activeElement).toBe(box);
    expect(bar()).toBeNull(); // the sheet takes the bar's place

    box.value = 'Is the 3rd firm?';
    box.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    sheet()!.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    await tick();
    await Promise.resolve();
    flushSync();
    expect(posted).toHaveLength(1);
    expect(posted[0].text).toBe('Is the 3rd firm?');
    expect(posted[0].sel.text).toBe('beta');
    expect(posted[0].sel.from).toBe(12);
    expect(posted[0].sel.to).toBe(16);
    expect(sheet()).toBeNull();
    expect(bar()).toBeNull(); // focus left the bar for the sheet: nothing keeps it up
  });

  it('Cancel closes the sheet and gives the editor its selection back', async () => {
    const { root, posted } = setup({ coarse: true });
    select(root, 12, 16);
    barButton('Comment on the selection').click();
    flushSync();
    await tick();
    const cancel = [...sheet()!.querySelectorAll('button')].find((b) => b.textContent!.trim() === 'Cancel')!;
    cancel.click();
    flushSync();
    expect(sheet()).toBeNull();
    expect(posted).toEqual([]);
    expect(document.activeElement).toBe(root);
    expect(getSelectionOffsets(root)).toEqual({ anchor: 12, focus: 16 });
  });

  it('Escape in the sheet closes it without posting', async () => {
    const { root, said } = setup({ coarse: true });
    select(root, 12, 16);
    barButton('Comment on the selection').click();
    flushSync();
    await tick();
    sheet()!.querySelector('textarea')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
    flushSync();
    expect(sheet()).toBeNull();
    expect(said).toEqual([]);
  });

  it('falls back to the host composer when the host cannot post', async () => {
    const { root, composed } = setup({ coarse: true, post: false });
    select(root, 12, 16);
    barButton('Comment on the selection').click();
    flushSync();
    expect(sheet()).toBeNull();
    expect(composed.map((c) => c.text)).toEqual(['beta']);
  });

  it('formats the remembered selection when the tap took it', () => {
    const { root, doc } = setup({ coarse: true });
    select(root, 12, 16);
    const bold = barButton('Bold');
    bold.focus();
    document.getSelection()!.removeAllRanges();
    bold.click();
    flushSync();
    expect(document.activeElement).toBe(root);
    expect(root.querySelector('strong, b')?.textContent).toBe('beta');
    void doc;
  });
});

describe('the touch bar on a finger', () => {
  it('cancels touchstart (no tap gesture: no focus change) and acts on touchend, once', () => {
    pointer(true);
    target = document.body.appendChild(document.createElement('div'));
    const oncomment = vi.fn();
    const noop = () => {};
    component = mount(TouchBar, {
      target,
      props: { active: { kind: null, bold: false, italic: false }, visible: true, readOnly: false, canComment: true, ontoggle: noop, onbullet: noop, ontask: noop, onslash: noop, oncomment }
    });
    flushSync();
    const btn = barButton('Comment on the selection');
    const touch = (type: string) => {
      const e = new Event(type, { bubbles: true, cancelable: true });
      Object.defineProperty(e, 'touches', { value: type === 'touchend' ? [] : [{ clientX: 0, clientY: 0 }] });
      Object.defineProperty(e, 'changedTouches', { value: [{ clientX: 0, clientY: 0 }] });
      btn.dispatchEvent(e);
      return e;
    };
    expect(touch('touchstart').defaultPrevented).toBe(true);
    expect(oncomment).not.toHaveBeenCalled();
    expect(touch('touchend').defaultPrevented).toBe(true);
    expect(oncomment).toHaveBeenCalledTimes(1);
    btn.click(); // a click some browsers still send after the touch: the same tap
    expect(oncomment).toHaveBeenCalledTimes(1);
  });
});

describe('caretScroll', () => {
  const view = { viewTop: 0, viewHeight: 500, bar: 50 };
  it('leaves a caret between the visible top and the bar', () => {
    expect(caretScroll({ top: 100, bottom: 120, ...view })).toBe(0);
    expect(caretScroll({ top: 418, bottom: 438, ...view })).toBe(0); // exactly 12 px above the bar
  });
  it('brings a caret under the bar up to just above it', () => {
    expect(caretScroll({ top: 460, bottom: 480, ...view })).toBe(480 - (500 - 50 - 12));
  });
  it('follows the visual viewport (keyboard up, page scrolled inside it)', () => {
    expect(caretScroll({ top: 700, bottom: 720, viewTop: 300, viewHeight: 400, bar: 50 })).toBe(720 - (300 + 400 - 50 - 12));
  });
  it('brings a caret above the visible top down', () => {
    expect(caretScroll({ top: 280, bottom: 300, viewTop: 300, viewHeight: 400, bar: 50 })).toBe(280 - 312);
  });
  it('never pushes the top of a tall caret out of view', () => {
    expect(caretScroll({ top: 20, bottom: 600, ...view })).toBe(8);
  });
});
