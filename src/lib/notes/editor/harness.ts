// A development harness for the notes editor: a local document (no backend) behind a NotesHost
// (ui/types.ts) that the page hands to the shared NotesEditor.svelte, plus a second replica behind
// a fake relay so a "remote" edit can be driven from the console or a test, fake inserts and a
// fake presence (carets fed from the test). Exposed as `window.__notes` by the dev-only route
// src/routes/dev/notes-editor (tests-e2e/notes-editor.spec.ts and the WebKitGTK probe use it).
// Only ever imported under `import.meta.env.DEV`: production builds drop it.
import { insertModel, insertText, setLink, type NotesModel } from '../commands.js';
import type { Chip } from '../chips.js';
import { ensureTerminated, lines, openDoc, type Doc, type Line } from '../model.js';
import { parseMarkdown, renderMarkdown } from '../pkg/notes_wasm.js';
import { applyRemote, drain } from '../sync.js';
import type { CaretView, NotesHost, PresenceSource, RemoteHooks, SelectionIds } from '../ui/types.js';
import { WasmHashWeb, ensureWasm } from '../wasm.js';
import { selectionModel } from './clipboard.js';
import { extractText, getSelectionOffsets, setSelectionOffsets } from './dom.js';
import type { LinkRequest } from './input.js';

const ORIGIN = 'c1'.repeat(32);
const PEOPLE = [{ name: 'Ada Lovelace' }, { name: 'Bob Smith' }];
const MEETINGS = [{ id: '0123456789ab', label: 'Planning · Oct 14' }];

/** What the page supplies: the (reactive) read-only flag the host reports. */
export interface HarnessControl {
  get(): boolean;
  set(v: boolean): void;
}

export interface Harness {
  web: WasmHashWeb;
  doc: Doc;
  /** The host for NotesEditor (its `readOnly` reads the page's control). */
  host: NotesHost;
  /** The editor's root element (data-testid="editor"), once mounted. */
  root(): HTMLElement;
  /** The old view surface, over the DOM: setSelection and the document's lines. */
  view: {
    setSelection(anchor: number, focus?: number, preferAfter?: boolean): void;
    readonly lines: Line[];
  };
  /** The old input surface: read-only and the link prompt (the editor's own form is driven). */
  input: {
    setReadOnly(v: boolean): void;
    setLink(from: number, to: number, url: string | null): void;
  };
  /** The second replica (the "other person"). */
  remote: { web: WasmHashWeb; doc: Doc };
  text(): string;
  lines(): Line[];
  /** The document's canonical markdown. */
  markdown(): string;
  /** Insert plain text on the other replica and deliver it here (through the editor's remote hooks). */
  remoteInsert(at: number, text: string): void;
  /** Deliver this replica's frames to the other one (also done after every local change). */
  flush(): void;
  log: string[];
  parseMarkdown: typeof parseMarkdown;
  renderMarkdown: typeof renderMarkdown;
  extractText: typeof extractText;
  getSelectionOffsets: typeof getSelectionOffsets;
  /** Link prompts the editor opened (Ctrl+K): the selection and the link under it. */
  linkRequests: { from: number; to: number; url: string | null }[];
  /** The last chip clicked (InsertProvider.onChipClick). */
  lastChip: Chip | null;
  /** Messages the editor gave to host.say. */
  said: { message: string; level: 'info' | 'error' }[];
  /** Show (or move) another person's caret/selection, anchors computed from offsets. */
  setRemoteCaret(key: string, name: string, sel: { anchor: number; focus?: number }): void;
  removeRemoteCaret(key: string): void;
  clearRemoteCarets(): void;
  /** Our own selection as the editor last sent it to presence (null: none). */
  localSel: SelectionIds | null;
}

export async function mountHarness(markdown: string, control: HarnessControl): Promise<Harness> {
  await ensureWasm();
  const web = new WasmHashWeb();
  const doc = openDoc(web, ORIGIN);
  const other = new WasmHashWeb();
  const remote = { web: other, doc: openDoc(other, ORIGIN) };
  // A parsed model ends in "\n", so it terminates the doc by itself.
  if (markdown) insertModel(doc, 0, JSON.parse(parseMarkdown(markdown)) as NotesModel);
  ensureTerminated(doc);
  const log: string[] = [];
  const flush = () => {
    for (const f of drain(web)) {
      const { send } = applyRemote(other, f);
      for (const back of send) applyRemote(web, back);
    }
  };
  flush(); // the seed reaches the other replica, so its edits merge in place

  let hooks: RemoteHooks | null = null;
  const inRemote = (apply: () => void) => {
    if (hooks) hooks.wrap(apply);
    else apply();
    hooks?.after();
  };

  const rootEl = () => document.querySelector<HTMLElement>('[data-testid=editor]')!;

  // ---- the link prompt: the editor opens its own form; record it and drive it -------------------
  const linkRequests: Harness['linkRequests'] = [];
  let keySel: { anchor: number; focus: number } | null = null;
  if (typeof document !== 'undefined') {
    // Before the input layer sees Ctrl+K, so the selection is still the one the prompt is for.
    document.addEventListener(
      'keydown',
      (e) => {
        if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === 'k') {
          keySel = getSelectionOffsets(rootEl());
        }
      },
      true
    );
    new MutationObserver(() => {
      const form = document.querySelector('form[aria-label=Link]');
      if (form && !form.hasAttribute('data-recorded')) {
        form.setAttribute('data-recorded', '');
        const field = form.querySelector<HTMLInputElement>('input');
        const sel = keySel ?? { anchor: 0, focus: 0 };
        const r = {
          from: Math.min(sel.anchor, sel.focus),
          to: Math.max(sel.anchor, sel.focus),
          url: field?.value || null
        };
        linkRequests.push(r);
        log.push(`link ${r.from}-${r.to} ${r.url ?? ''}`);
      }
    }).observe(document.body, { childList: true, subtree: true });
  }

  // ---- fake presence ------------------------------------------------------------------------------
  const carets = new Map<string, CaretView>();
  const listeners = new Set<() => void>();
  const notify = () => listeners.forEach((fn) => fn());
  const idAfter = (off: number): string | null => (off <= 0 ? null : (web.seqIdAt(doc.obj, off - 1) ?? null));
  const presence: PresenceSource = {
    setSel(sel) {
      h.localSel = sel;
    },
    carets: () => [...carets.values()],
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    }
  };

  const h: Harness = {
    web,
    doc,
    host: {
      doc,
      get readOnly() {
        return control.get();
      },
      canComment: false,
      undoButtons: false,
      openComments: new Set<string>(),
      localChange() {
        log.push('change');
        flush();
      },
      setTyping: (on) => {
        if (on) log.push('typing');
      },
      flush,
      bindRemote(hk) {
        hooks = hk;
        return () => {
          if (hooks === hk) hooks = null;
        };
      },
      compose: () => {},
      onMark: () => {},
      say(message, level = 'info') {
        h.said.push({ message, level });
        log.push(`say ${message}`);
      },
      presence,
      inserts: {
        time: () => ({ part: 0, seconds: 754, label: '12:34' }),
        people: async (q) => PEOPLE.filter((p) => p.name.toLowerCase().includes(q.trim().toLowerCase())),
        meetings: async (q) =>
          MEETINGS.filter((m) => m.label.toLowerCase().includes(q.trim().toLowerCase())),
        onChipClick: (chip) => {
          h.lastChip = chip;
          log.push(`chip ${chip.kind}`);
        }
      }
    },
    root: rootEl,
    view: {
      setSelection: (anchor, focus = anchor, preferAfter = false) =>
        setSelectionOffsets(rootEl(), anchor, focus, preferAfter),
      get lines() {
        return lines(doc);
      }
    },
    input: {
      setReadOnly: (v) => control.set(v),
      setLink(from, to, url) {
        const form = document.querySelector<HTMLFormElement>('form[aria-label=Link]');
        if (form) {
          if (url === null) {
            form.querySelector<HTMLButtonElement>('button[type=button]')?.click();
          } else {
            const field = form.querySelector<HTMLInputElement>('input')!;
            field.value = url;
            field.dispatchEvent(new Event('input', { bubbles: true }));
            form.requestSubmit();
          }
          return;
        }
        inRemote(() => setLink(doc, from, to, url));
        h.host.localChange();
      }
    },
    remote,
    text: () => web.text(doc.obj),
    lines: () => lines(doc),
    markdown: () => {
      const ls = lines(doc);
      const end = ls.length ? ls[ls.length - 1].end + 1 : 0;
      return renderMarkdown(JSON.stringify(selectionModel(ls, 0, end)));
    },
    remoteInsert(at, text) {
      insertText(remote.doc, at, text);
      for (const f of drain(other)) inRemote(() => applyRemote(web, f));
    },
    flush,
    log,
    parseMarkdown,
    renderMarkdown,
    extractText,
    getSelectionOffsets,
    linkRequests,
    lastChip: null,
    said: [],
    setRemoteCaret(key, name, sel) {
      const f = sel.focus ?? sel.anchor;
      carets.set(key, { key, name, who: name, sel: { a: idAfter(sel.anchor), f: idAfter(f) } });
      notify();
    },
    removeRemoteCaret(key) {
      if (carets.delete(key)) notify();
    },
    clearRemoteCarets() {
      carets.clear();
      notify();
    },
    localSel: null
  };
  return h;
}
