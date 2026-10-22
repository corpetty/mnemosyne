// A development harness for the notes editor: a local document (no backend) mounted with the
// DOM and input layers, plus a second replica behind a fake relay so a "remote" edit can be
// driven from the console or a test. Exposed as `window.__notes` by the dev-only route
// src/routes/dev/notes-editor (tests-e2e/notes-editor.spec.ts and the WebKitGTK probe use it).
// Only ever imported under `import.meta.env.DEV`: production builds drop it.
import { insertModel, insertText, toggleTask, type NotesModel } from '../commands.js';
import { ensureTerminated, lines, openDoc, type Doc, type Line } from '../model.js';
import { parseMarkdown, renderMarkdown } from '../pkg/notes_wasm.js';
import { applyRemote, drain } from '../sync.js';
import { UndoStack } from '../undo.js';
import { WasmHashWeb, ensureWasm } from '../wasm.js';
import { selectionModel } from './clipboard.js';
import { extractText, getSelectionOffsets } from './dom.js';
import { attachInput, type EditorInput, type LinkRequest } from './input.js';
import { createEditorView, type EditorView } from './view.js';

const ORIGIN = 'c1'.repeat(32);

export interface Harness {
  web: WasmHashWeb;
  doc: Doc;
  view: EditorView;
  input: EditorInput;
  undo: UndoStack;
  /** The second replica (the "other person"). */
  remote: { web: WasmHashWeb; doc: Doc };
  text(): string;
  lines(): Line[];
  /** The document's canonical markdown. */
  markdown(): string;
  /** Insert plain text on the other replica and deliver it here (through applyRemote). */
  remoteInsert(at: number, text: string): void;
  /** Deliver this replica's frames to the other one (also done after every local change). */
  flush(): void;
  log: string[];
  parseMarkdown: typeof parseMarkdown;
  renderMarkdown: typeof renderMarkdown;
  extractText: typeof extractText;
  getSelectionOffsets: typeof getSelectionOffsets;
  linkRequests: LinkRequest[];
}

export async function mountHarness(root: HTMLElement, markdown: string): Promise<Harness> {
  await ensureWasm();
  const web = new WasmHashWeb();
  const doc = openDoc(web, ORIGIN);
  const other = new WasmHashWeb();
  const remote = { web: other, doc: openDoc(other, ORIGIN) };
  // A parsed model ends in "\n", so it terminates the doc by itself.
  if (markdown) insertModel(doc, 0, JSON.parse(parseMarkdown(markdown)) as NotesModel);
  ensureTerminated(doc);
  const log: string[] = [];
  const undo = new UndoStack(doc);
  const view = createEditorView({
    root,
    doc,
    onToggleTask: (i) => {
      undo.push(toggleTask(doc, view.lines[i].start).inverse);
      flush();
      view.update();
    }
  });
  const linkRequests: LinkRequest[] = [];
  const flush = () => {
    for (const f of drain(web)) {
      const { send } = applyRemote(other, f);
      for (const back of send) applyRemote(web, back);
    }
  };
  const input = attachInput(view, doc, {
    undo,
    onLocalChange: () => {
      log.push('change');
      flush();
    },
    onTyping: () => log.push('typing'),
    requestLink: (r) => {
      linkRequests.push(r);
      log.push(`link ${r.from}-${r.to} ${r.url ?? ''}`);
    },
    onPasteTooLarge: (n) => log.push(`paste-too-large ${n}`)
  });
  view.update();
  flush(); // the seed reaches the other replica, so its edits merge in place
  const h: Harness = {
    web,
    doc,
    view,
    input,
    undo,
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
      for (const f of drain(other)) input.applyRemote(() => applyRemote(web, f));
    },
    flush,
    log,
    parseMarkdown,
    renderMarkdown,
    extractText,
    getSelectionOffsets,
    linkRequests
  };
  return h;
}
