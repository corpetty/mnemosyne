// Clipboard content for the notes editor: a selection as a NotesModel and its markdown (what
// copy and cut put on the clipboard), and a pasted text/plain parsed as markdown into the
// plan the input layer applies (phase 6 design: "copy puts the selection's markdown on the
// clipboard; text/plain is parsed as markdown by notes-core in wasm").
import type { NotesModel } from '../commands.js';
import { CELL_SEP, cpLen, cpSlice, type Line } from '../model.js';
import { parseMarkdown, renderMarkdown } from '../pkg/notes_wasm.js';
import { FILLER } from './points.js';

/** Pastes longer than this many code points are refused (`onPasteTooLarge`). */
export const PASTE_LIMIT = 40_000;

/**
 * The lines' content over `[from, to)` as a model, inline marks clipped. A selection inside
 * one line is an inline fragment (no line entry); one over several lines carries every
 * line's kind, the last fragment terminated too, so a partial last line keeps its kind.
 */
export function selectionModel(ls: readonly Line[], from: number, to: number): NotesModel {
  const model: NotesModel = { text: '', lines: [], inline: [] };
  const entry = (line: Line) => ({
    kind: line.kind,
    ...(line.lang ? { lang: line.lang } : {}),
    ...(line.align ? { align: line.align } : {})
  });
  let last: Line | null = null;
  for (const line of ls) {
    if (line.end < from || line.start >= to) continue;
    const a = Math.max(from, line.start);
    const b = Math.min(to, line.end);
    let off = line.start;
    // Doc offset x inside this line lands at output offset base + x.
    const base = cpLen(model.text) - a;
    for (const r of line.runs) {
      const n = cpLen(r.text);
      const ra = Math.max(off, a);
      const rb = Math.min(off + n, b);
      if (ra < rb) {
        for (const kind of ['bold', 'italic', 'strike', 'code'] as const) {
          if (r[kind]) model.inline.push({ from: base + ra, to: base + rb, kind, value: 'true' });
        }
        if (r.link) model.inline.push({ from: base + ra, to: base + rb, kind: 'link', value: r.link });
      }
      off += n;
    }
    model.text += cpSlice(line.text, a - line.start, b - line.start);
    last = line;
    if (to > line.end && line.id !== null) {
      model.text += '\n';
      model.lines.push(entry(line));
    }
  }
  if (last && model.lines.length > 0 && !model.text.endsWith('\n')) {
    model.text += '\n';
    model.lines.push(entry(last));
  }
  return model;
}

/** The markdown of a selection: a partial table row is plain text with " | " between cells. */
export function selectionMarkdown(ls: readonly Line[], from: number, to: number): string {
  const model = selectionModel(ls, from, to);
  if (!model.text.includes('\n') && model.text.includes(CELL_SEP)) return model.text.split(CELL_SEP).join(' | ');
  const md = renderMarkdown(JSON.stringify(model));
  const endsOnTerminator = ls.some((l) => l.id !== null && l.end === to - 1 && l.end >= from);
  return endsOnTerminator ? md : md.replace(/\n$/, '');
}

/** The selected DOM as HTML, fillers and chrome stripped (a simple text/html for other apps). */
export function selectionHtml(root: HTMLElement): string {
  const sel = root.ownerDocument.getSelection();
  if (!sel || sel.rangeCount === 0) return '';
  const range = sel.getRangeAt(0);
  if (!root.contains(range.commonAncestorContainer)) return '';
  const box = root.ownerDocument.createElement('div');
  box.appendChild(range.cloneContents());
  for (const el of Array.from(box.querySelectorAll('[data-chrome]'))) el.remove();
  const walker = root.ownerDocument.createTreeWalker(box, NodeFilter.SHOW_TEXT);
  const texts: Text[] = [];
  let t: Node | null;
  while ((t = walker.nextNode())) texts.push(t as Text);
  for (const text of texts) text.data = text.data.replaceAll(FILLER, '');
  return box.innerHTML;
}

/** Line endings normalised, fillers removed. */
export function cleanPaste(raw: string): string {
  return raw.replace(/\r\n?/g, '\n').replaceAll(FILLER, '');
}

/** What a paste into a table cell becomes: one line, no separators. */
export function cellPasteText(text: string): string {
  return text.replace(/[\n\u001f]+/g, ' ').trim();
}

export type PastePlan =
  /** One plain paragraph (marks allowed): inserted at the caret without splitting the line. */
  | { kind: 'inline'; model: NotesModel }
  /** Line structure: inserted as whole lines. */
  | { kind: 'block'; model: NotesModel };

export function parsePaste(text: string): PastePlan {
  const model = JSON.parse(parseMarkdown(text)) as NotesModel;
  const only = model.lines.length === 1 ? model.lines[0] : null;
  if (model.lines.length === 0 || (only && !only.kind && !only.lang)) {
    return { kind: 'inline', model: { text: model.text.replace(/\n$/, ''), lines: [], inline: model.inline } };
  }
  return { kind: 'block', model };
}
