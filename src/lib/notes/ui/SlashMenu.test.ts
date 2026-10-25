// The `/` menu in jsdom: NotesEditor mounted on a local document with fake inserts, driven by
// native typing (as input.test.ts does it) and the editor's own keydown.
import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';
import { personUrl, timeUrl } from '../chips.js';
import { insertText } from '../commands.js';
import { getSelectionOffsets, setSelectionOffsets } from '../editor/dom.js';
import { ensureTerminated, lines, openDoc, type Doc } from '../model.js';
import { ORIGIN, view as viewOf, WasmHashWeb } from '../test-harness.js';
import NotesEditor from './NotesEditor.svelte';
import type { InsertProvider, NotesHost } from './types.js';

// jsdom has no ResizeObserver (the caret overlay uses one).
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver;

let target: HTMLElement;
let component: ReturnType<typeof mount> | null = null;

afterEach(() => {
  if (component) unmount(component);
  component = null;
  target?.remove();
});

interface Setup {
  doc: Doc;
  root: HTMLElement;
  queries: string[];
}

const PEOPLE = [{ name: 'Ana', hint: 'ana@example.com' }, { name: 'Bob' }, { name: 'Ana Lima' }];

function setup(text = '', inserts: InsertProvider | null = null): Setup {
  const doc = openDoc(new WasmHashWeb(), ORIGIN);
  ensureTerminated(doc);
  if (text) insertText(doc, 0, text);
  const queries: string[] = [];
  const ins: InsertProvider = inserts ?? {
    time: () => ({ part: 1, seconds: 125, label: '2:05' }),
    people: async (q) => {
      queries.push(q);
      return PEOPLE.filter((p) => p.name.toLowerCase().includes(q.toLowerCase()));
    },
    meetings: async () => [{ id: '0123abcd4567', label: 'Kickoff' }]
  };
  const host: NotesHost = {
    doc,
    readOnly: false,
    canComment: false,
    undoButtons: false,
    openComments: new Set(),
    localChange() {},
    setTyping() {},
    flush() {},
    bindRemote: () => () => {},
    compose() {},
    onMark() {},
    say() {},
    inserts: ins
  };
  target = document.createElement('div');
  document.body.appendChild(target);
  component = mount(NotesEditor, { target, props: { host, testid: 'ed', label: 'Notes' } });
  flushSync();
  const root = target.querySelector<HTMLElement>('[data-testid="ed"]')!;
  root.tabIndex = 0; // jsdom focuses a contenteditable only with a tabindex
  root.focus();
  const end = Math.max(0, doc.web.textLen(doc.obj) - 1);
  setSelectionOffsets(root, end, end);
  return { doc, root, queries };
}

/** What a browser does for a native keystroke: edit the text node under the caret, then `input`. */
function typeNative(root: HTMLElement, text: string): void {
  for (const ch of text) {
    const sel = document.getSelection() as Selection;
    const e = new InputEvent('beforeinput', { inputType: 'insertText', data: ch, bubbles: true, cancelable: true });
    root.dispatchEvent(e);
    if (e.defaultPrevented) continue;
    let node = sel.focusNode as Node;
    let offset = sel.focusOffset;
    if (node.nodeType !== Node.TEXT_NODE) {
      const t = document.createTextNode('');
      node.insertBefore(t, node.childNodes[offset] ?? null);
      node = t;
      offset = 0;
    }
    const t = node as Text;
    t.data = t.data.slice(0, offset) + ch + t.data.slice(offset);
    sel.setBaseAndExtent(t, offset + ch.length, t, offset + ch.length);
    root.dispatchEvent(new InputEvent('input', { inputType: 'insertText', data: ch, bubbles: true }));
    flushSync();
  }
}

function key(root: HTMLElement, k: string, init: Partial<KeyboardEventInit> = {}): KeyboardEvent {
  const e = new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true, ...init });
  root.dispatchEvent(e);
  flushSync();
  return e;
}

/** Let the menu place itself (tick) and the pickers answer (debounce). */
async function settle(ms = 0) {
  if (ms) await new Promise((r) => setTimeout(r, ms));
  await tick();
  await Promise.resolve();
  flushSync();
}

const menu = () => target.querySelector<HTMLElement>('[role="listbox"]');
const options = () => [...(menu()?.querySelectorAll<HTMLElement>('[role="option"]') ?? [])].map((o) => o.textContent!.replace(/\s+/g, ' ').trim());
const selectedOption = () => menu()?.querySelector<HTMLElement>('[aria-selected="true"]');
const text = (s: Setup) => s.doc.web.text(s.doc.obj);

describe('the / menu', () => {
  it('opens on / at a line start with both groups, the editor keeping focus', async () => {
    const s = setup();
    typeNative(s.root, '/');
    await settle();
    expect(menu()).not.toBeNull();
    expect([...menu()!.querySelectorAll('[role="group"]')].map((g) => g.getAttribute('aria-label'))).toEqual(['This meeting', 'Blocks']);
    expect(options()[0]).toBe('Time now 2:05');
    expect(options()).toContain('Heading 2');
    expect(document.activeElement).toBe(s.root);
    expect(s.root.getAttribute('aria-activedescendant')).toBe(selectedOption()!.id);
    expect(s.root.getAttribute('aria-controls')).toBe(menu()!.id);
    expect(s.root.contains(menu())).toBe(false);
  });

  it('filters by the query, moves with the arrows and picks a line kind in one undo entry', async () => {
    const s = setup('note ');
    typeNative(s.root, '/hea');
    await settle();
    expect(options()).toEqual(['Heading 1', 'Heading 2', 'Heading 3']);
    const first = selectedOption()!.id;
    key(s.root, 'ArrowDown');
    expect(selectedOption()!.textContent).toContain('Heading 2');
    expect(s.root.getAttribute('aria-activedescendant')).not.toBe(first);
    const enter = key(s.root, 'Enter');
    expect(enter.defaultPrevented).toBe(true);
    expect(menu()).toBeNull();
    expect(s.root.hasAttribute('aria-activedescendant')).toBe(false);
    expect(text(s)).toBe('note \n');
    expect(lines(s.doc)[0].kind).toBe('h2');
    expect(getSelectionOffsets(s.root)).toEqual({ anchor: 5, focus: 5 });
    key(s.root, 'z', { ctrlKey: true });
    expect(text(s)).toBe('note /hea\n');
    expect(lines(s.doc)[0].kind).toBeNull();
  });

  it('Escape closes and leaves the typed text; a space with no match closes', async () => {
    const s = setup();
    typeNative(s.root, '/he');
    await settle();
    expect(menu()).not.toBeNull();
    key(s.root, 'Escape');
    await settle();
    expect(menu()).toBeNull();
    expect(text(s)).toBe('/he\n');
    typeNative(s.root, ' /zz');
    await settle();
    expect(menu()).not.toBeNull();
    expect(options()).toEqual([]);
    key(s.root, ' ');
    await settle();
    expect(menu()).toBeNull();
  });

  it('does not open mid-word', async () => {
    const s = setup('and');
    typeNative(s.root, '/');
    await settle();
    expect(menu()).toBeNull();
  });

  it('closes when the caret leaves the query', async () => {
    const s = setup('ab ');
    typeNative(s.root, '/h');
    await settle();
    expect(menu()).not.toBeNull();
    setSelectionOffsets(s.root, 1, 1);
    document.dispatchEvent(new Event('selectionchange'));
    await settle();
    expect(menu()).toBeNull();
  });

  it('inserts a Table and the time now as a chip', async () => {
    const s = setup();
    typeNative(s.root, '/table');
    await settle();
    expect(options()[0]).toBe('Table');
    key(s.root, 'Tab');
    expect(lines(s.doc).slice(0, 2).every((l) => l.cells !== null)).toBe(true);
    const t = setup('at ');
    typeNative(t.root, '/now');
    await settle();
    key(t.root, 'Enter');
    expect(text(t)).toBe('at 2:05 \n');
    expect(viewOf(lines(t.doc))).toContain(`link=${timeUrl(1, 125)}`);
  });

  it('Ctrl+/ opens it at the caret without trigger text; the query typed after it goes on a pick', async () => {
    const s = setup('x');
    const e = key(s.root, '/', { ctrlKey: true, code: 'Slash' });
    expect(e.defaultPrevented).toBe(true);
    await settle();
    expect(menu()).not.toBeNull();
    typeNative(s.root, 'quo');
    await settle();
    expect(options()).toEqual(['Quote']);
    key(s.root, 'Enter');
    expect(text(s)).toBe('x\n');
    expect(lines(s.doc)[0].kind).toBe('quote');
  });

  it('the Insert button opens it', async () => {
    const s = setup();
    const btn = target.querySelector<HTMLButtonElement>('button[aria-label="Insert"]')!;
    expect(btn.disabled).toBe(false);
    btn.click();
    await settle();
    expect(menu()).not.toBeNull();
    expect(document.activeElement).toBe(s.root);
  });
});

describe('the pickers', () => {
  it('@ opens the person picker; the query is debounced, latest wins; a pick inserts a chip', async () => {
    const s = setup('ask ');
    typeNative(s.root, '@');
    await settle();
    expect(menu()?.getAttribute('aria-label')).toBe('People');
    expect(options()).toEqual(['Ana ana@example.com', 'Bob', 'Ana Lima']);
    typeNative(s.root, 'an');
    await settle(200);
    expect(s.queries).toEqual(['', 'an']); // "a" was superseded within the debounce
    expect(options()).toEqual(['Ana ana@example.com', 'Ana Lima']);
    key(s.root, 'ArrowDown');
    key(s.root, 'Enter');
    expect(text(s)).toBe('ask @Ana Lima \n');
    expect(viewOf(lines(s.doc))).toContain(`"@Ana Lima"{link=${personUrl('Ana Lima')}}`);
    key(s.root, 'z', { ctrlKey: true });
    expect(text(s)).toBe('ask @an\n');
  });

  it('[[ opens the meeting picker', async () => {
    const s = setup('see ');
    typeNative(s.root, '[[');
    await settle(10);
    expect(menu()?.getAttribute('aria-label')).toBe('Earlier meetings');
    key(s.root, 'Enter');
    expect(text(s)).toBe('see Kickoff \n');
    expect(viewOf(lines(s.doc))).toContain('link=mnemosyne:meeting/0123abcd4567');
  });

  it('Person from the menu switches the list into the picker and a pick removes the trigger too', async () => {
    const s = setup();
    typeNative(s.root, '/pers');
    await settle();
    expect(options()).toEqual(['Person']);
    key(s.root, 'Enter');
    await settle(10);
    expect(menu()?.getAttribute('aria-label')).toBe('People');
    typeNative(s.root, 'bo');
    await settle(200);
    expect(options()).toEqual(['Bob']);
    key(s.root, 'Enter');
    expect(text(s)).toBe('@Bob \n');
    key(s.root, 'z', { ctrlKey: true });
    expect(text(s)).toBe('/persbo\n');
  });

  it('Action item makes the line a task and puts the owner first', async () => {
    const s = setup('send the deck ');
    typeNative(s.root, '/action');
    await settle();
    key(s.root, 'Enter');
    await settle(10);
    expect(lines(s.doc)[0].kind).toBe('task');
    expect(text(s)).toBe('send the deck \n');
    expect(menu()?.getAttribute('aria-label')).toBe('People');
    expect(getSelectionOffsets(s.root)).toEqual({ anchor: 0, focus: 0 });
    key(s.root, 'Enter');
    expect(text(s)).toBe('@Ana send the deck \n');
    expect(lines(s.doc)[0].kind).toBe('task');
  });

  it('without providers: no @ picker and no This meeting inserts but Action item', async () => {
    const s = setup('', {});
    typeNative(s.root, '@');
    await settle();
    expect(menu()).toBeNull();
    typeNative(s.root, ' /');
    await settle();
    expect(options()[0]).toBe('Action item');
    expect(options()).not.toContain('Person');
  });
});
