// TEMPORARY verification (deleted before handing back): NotesEditor's image paths in jsdom.
import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { insertText } from '../commands.js';
import { setSelectionOffsets } from '../editor/dom.js';
import { ensureTerminated, lines, openDoc, type Doc } from '../model.js';
import { ORIGIN, WasmHashWeb } from '../test-harness.js';
import NotesEditor from './NotesEditor.svelte';
import type { MediaProvider, NotesHost, PreparedImage } from './types.js';

globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver;

let target: HTMLElement;
let component: ReturnType<typeof mount> | null = null;
let webpBytes: number[];

beforeEach(() => {
  webpBytes = [0x52, 0x49, 0x46, 0x46, 4, 0, 0, 0, 0x57, 0x45, 0x42, 0x50, 1];
  vi.stubGlobal('createImageBitmap', async (f: Blob) => {
    if (f.type === 'image/heic') throw new Error('nope');
    return { width: 800, height: 600, close() {} };
  });
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation((() => ({ drawImage() {}, fillRect() {} })) as never);
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(function (cb: BlobCallback, type?: string) {
    const b = new Blob([new Uint8Array(webpBytes)], { type: type ?? '' });
    Object.defineProperty(b, 'arrayBuffer', { value: async () => new Uint8Array(webpBytes).buffer });
    setTimeout(() => cb(b), 0);
  });
});

afterEach(() => {
  if (component) unmount(component);
  component = null;
  target?.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

interface Fake extends MediaProvider {
  calls: PreparedImage[];
  fail: boolean;
  urls: Map<string, string>;
  fire(sha: string): void;
  missing: Set<string>;
}

function fakeMedia(): Fake {
  const ls = new Set<(sha: string) => void>();
  const m: Fake = {
    calls: [],
    fail: false,
    urls: new Map(),
    missing: new Set(),
    url: (sha) => m.urls.get(sha) ?? null,
    state: (sha) => (m.missing.has(sha) ? 'missing' : null),
    subscribe(fn) {
      ls.add(fn);
      return () => ls.delete(fn);
    },
    fire: (sha) => ls.forEach((f) => f(sha)),
    async upload(p) {
      m.calls.push(p);
      await new Promise((r) => setTimeout(r, 5));
      if (m.fail) throw new Error('x');
    }
  };
  return m;
}

function setup(text: string, media: MediaProvider | undefined, readOnly = false) {
  const doc = openDoc(new WasmHashWeb(), ORIGIN);
  ensureTerminated(doc);
  if (text) insertText(doc, 0, text);
  const said: string[] = [];
  const host: NotesHost = {
    doc,
    readOnly,
    canComment: false,
    undoButtons: false,
    openComments: new Set(),
    localChange() {},
    setTyping() {},
    flush() {},
    bindRemote: () => () => {},
    compose() {},
    onMark() {},
    say: (m) => said.push(m),
    media
  };
  target = document.createElement('div');
  document.body.appendChild(target);
  component = mount(NotesEditor, { target, props: { host, testid: 'ed', label: 'Notes' } });
  flushSync();
  const root = target.querySelector<HTMLElement>('[data-testid="ed"]')!;
  root.tabIndex = 0;
  root.focus();
  const end = Math.max(0, doc.web.textLen(doc.obj) - 1);
  setSelectionOffsets(root, end, end);
  return { doc, root, said };
}

function pasteFiles(root: HTMLElement, files: File[], text = '') {
  const event = new Event('paste', { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'clipboardData', { value: { files, items: [], getData: () => text, setData() {} } });
  root.dispatchEvent(event);
}

const wait = (ms = 30) => new Promise((r) => setTimeout(r, ms));
const png = (type = 'image/png') => new File([new Uint8Array([1])], 'a.png', { type });
const fig = (root: HTMLElement) => root.querySelector<HTMLElement>('figure.ne-image');
const kinds = (doc: Doc) => lines(doc).map((l) => `${l.kind}:${l.text}`);

describe('images in NotesEditor', () => {
  it('paste → line at once, upload, failed → Retry / Remove, undo', async () => {
    const m = fakeMedia();
    m.fail = true;
    const s = setup('abc', m);
    pasteFiles(s.root, [png()]);
    for (let i = 0; i < 50 && !m.calls.length; i++) await wait(1);
    await tick();
    flushSync();
    expect(kinds(s.doc)).toEqual(['null:abc', 'image:']);
    expect(m.calls.length).toBe(1);
    let f = fig(s.root)!;
    expect(f.dataset.state).toBe('uploading');
    expect(f.textContent).toBe('Uploading…');
    await wait();
    f = fig(s.root)!;
    expect(f.dataset.state).toBe('failed');
    expect([...f.querySelectorAll('button')].map((b) => b.textContent)).toEqual(['Retry', 'Remove']);
    m.fail = false;
    f.querySelector<HTMLButtonElement>('button[data-act=retry]')!.click();
    expect(m.calls.length).toBe(2);
    expect(m.calls[1]).toBe(m.calls[0]);
    await wait();
    expect(fig(s.root)!.dataset.state).toBe('loading');
    // bytes arrive
    m.urls.set(m.calls[0].sha, 'blob:x');
    m.fire(m.calls[0].sha);
    const img = fig(s.root)!.querySelector('img')!;
    expect(img.getAttribute('src')).toBe('blob:x');
    Object.defineProperty(img, 'complete', { value: true, configurable: true });
    Object.defineProperty(img, 'naturalWidth', { value: 800, configurable: true });
    img.dispatchEvent(new Event('load'));
    expect(fig(s.root)!.dataset.state).toBe('ready');
    expect(fig(s.root)!.getAttribute('tabindex')).toBe('0');
    // lightbox
    fig(s.root)!.click();
    flushSync();
    const lb = document.querySelector<HTMLElement>('[data-testid=notes-lightbox]')!;
    expect(lb.getAttribute('role')).toBe('dialog');
    expect(lb.getAttribute('aria-label')).toBe('Image');
    expect(lb.querySelector('img')!.getAttribute('src')).toBe('blob:x');
    expect(document.activeElement?.getAttribute('aria-label')).toBe('Close image');
    lb.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
    flushSync();
    expect(document.querySelector('[data-testid=notes-lightbox]')).toBeNull();
    expect(document.activeElement).toBe(fig(s.root));
    // Enter on focused figure opens again; backdrop click closes
    fig(s.root)!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    flushSync();
    const lb2 = document.querySelector<HTMLElement>('[data-testid=notes-lightbox]')!;
    expect(lb2).not.toBeNull();
    lb2.click();
    flushSync();
    expect(document.querySelector('[data-testid=notes-lightbox]')).toBeNull();
    expect(kinds(s.doc)).toEqual(['null:abc', 'image:']);
  });

  it('remove and undo; missing; refusals; no media', async () => {
    const m = fakeMedia();
    m.fail = true;
    const s = setup('abc', m);
    vi.stubGlobal('URL', Object.assign(Object.create(URL), { createObjectURL: undefined }));
    pasteFiles(s.root, [png(), png('image/heic'), png()]);
    await wait(60);
    flushSync();
    expect(s.said).toEqual(['That image format cannot be read here']);
    expect(kinds(s.doc)).toEqual(['null:abc', 'image:', 'image:']);
    fig(s.root)!.querySelector<HTMLButtonElement>('button[data-act=remove]')!.click();
    flushSync();
    expect(kinds(s.doc)).toEqual(['null:abc', 'image:']);
    s.root.dispatchEvent(new KeyboardEvent('keydown', { key: 'z', ctrlKey: true, bubbles: true, cancelable: true }));
    expect(kinds(s.doc)).toEqual(['null:abc', 'image:', 'image:']);
    m.missing.add(m.calls[0].sha);
    m.fire(m.calls[0].sha);
    // still failed (local) wins
    expect(fig(s.root)!.dataset.state).toBe('failed');

    component && unmount(component);
    component = null;
    target.remove();
    const n = setup('abc', undefined);
    pasteFiles(n.root, [png()], 'xy');
    await wait(10);
    expect(n.doc.web.text(n.doc.obj)).toBe('abcxy\n');
  });

  it('missing from the provider shows Image not available, read-only no buttons; slash Image', async () => {
    const m = fakeMedia();
    const sha = 'a'.repeat(64);
    m.missing.add(sha);
    const doc = openDoc(new WasmHashWeb(), ORIGIN);
    ensureTerminated(doc);
    const { insertImageLine } = await import('../commands.js');
    insertImageLine(doc, 0, `${sha}.webp@10x10`);
    const said: string[] = [];
    const host: NotesHost = {
      doc, readOnly: true, canComment: false, undoButtons: false, openComments: new Set(),
      localChange() {}, setTyping() {}, flush() {}, bindRemote: () => () => {}, compose() {}, onMark() {},
      say: (x) => said.push(x), media: m
    };
    target = document.createElement('div');
    document.body.appendChild(target);
    component = mount(NotesEditor, { target, props: { host, testid: 'ed' } });
    flushSync();
    await tick();
    const root = target.querySelector<HTMLElement>('[data-testid="ed"]')!;
    expect(fig(root)!.dataset.state).toBe('missing');
    expect(fig(root)!.textContent).toBe('Image not available');
    expect(fig(root)!.querySelectorAll('button').length).toBe(0);
    expect(target.querySelector('input[type=file]')).toBeNull();
  });

  it('slash Image picks through the hidden input', async () => {
    const m = fakeMedia();
    const s = setup('x\n', m);
    const input = target.querySelector<HTMLInputElement>('input[type=file]')!;
    expect(input.getAttribute('capture')).toBe('environment');
    expect(input.accept).toBe('image/*');
    let clicked = 0;
    input.click = () => clicked++;
    // type /ima natively
    for (const ch of '/ima') {
      const sel = document.getSelection() as Selection;
      const e = new InputEvent('beforeinput', { inputType: 'insertText', data: ch, bubbles: true, cancelable: true });
      s.root.dispatchEvent(e);
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
      sel.setBaseAndExtent(t, offset + 1, t, offset + 1);
      s.root.dispatchEvent(new InputEvent('input', { inputType: 'insertText', data: ch, bubbles: true }));
      flushSync();
    }
    await tick();
    flushSync();
    const opts = [...target.querySelectorAll('[role=option]')].map((o) => o.textContent!.trim());
    expect(opts).toEqual(['Image']);
    s.root.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    flushSync();
    expect(clicked).toBe(1);
    expect(s.doc.web.text(s.doc.obj)).toBe('x\n\n');
    Object.defineProperty(input, 'files', { value: [png()], configurable: true });
    input.dispatchEvent(new Event('change', { bubbles: true }));
    for (let i = 0; i < 50 && !m.calls.length; i++) await wait(1);
    expect(kinds(s.doc)).toEqual(['null:x', 'image:']);
  });
});
