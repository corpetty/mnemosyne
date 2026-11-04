// The Copilot's "Add to notes": one line of notes for a copilot item, as a model (for the live
// document) and as markdown (for the plain-text notes), and the insert that appends it.
import { byUrl } from './chips.js';
import { insertModel, insertText, setLineKind, setLink, type CommandResult, type NotesModel } from './commands.js';
import { cpLen, ensureTerminated, lines, type Doc, type LineKind } from './model.js';

/** Every copilot line starts with this chip and one space (the shape the backend writes too). */
const CHIP = 'Copilot';
const LEAD = CHIP + ' ';

export type CopilotItem =
  | { kind: 'summary'; text: string }
  | { kind: 'decision'; text: string }
  | { kind: 'action'; text: string; owner?: string | null }
  | { kind: 'question'; text: string };

/** The line's kind and text: summary point = bullet, "Decided: ", a task "text (owner)", "Question: ". */
export function copilotLine(item: CopilotItem): { kind: LineKind; text: string } {
  const clean = (s: string) => s.replace(/\s+/g, ' ').trim();
  const text = clean(item.text);
  switch (item.kind) {
    case 'summary':
      return { kind: 'bullet', text };
    case 'decision':
      return { kind: 'bullet', text: `Decided: ${text}` };
    case 'action': {
      const owner = clean(item.owner ?? '');
      return { kind: 'task', text: owner ? `${text} (${owner})` : text };
    }
    case 'question':
      return { kind: 'bullet', text: `Question: ${text}` };
  }
}

/** The item as a one-line NotesModel (text ends in the line's "\n"). */
export function copilotModel(item: CopilotItem): NotesModel {
  const { kind, text } = copilotLine(item);
  return {
    text: LEAD + text + '\n',
    lines: [{ kind }],
    inline: [{ from: 0, to: cpLen(CHIP), kind: 'link', value: byUrl('copilot') }]
  };
}

/** The item as a markdown line, for the plain-text notes. */
export function copilotMarkdown(item: CopilotItem): string {
  const { kind, text } = copilotLine(item);
  return (kind === 'task' ? '- [ ] ' : '- ') + `[${CHIP}](${byUrl('copilot')}) ` + text;
}

/** Plain-text notes with the item added as a new last line. */
export function appendToText(notes: string, item: CopilotItem): string {
  const line = copilotMarkdown(item);
  if (!notes.trim()) return line + '\n';
  return notes + (notes.endsWith('\n') ? '' : '\n') + line + '\n';
}

/**
 * Append the item to the document as one new line at the end. A last line that is empty (a
 * new document, or the user left a blank line) is filled instead of leaving a gap. The caller
 * sends the change (`localChange()`).
 */
export function appendToDoc(doc: Doc, item: CopilotItem): CommandResult {
  ensureTerminated(doc);
  const ls = lines(doc);
  const last = ls[ls.length - 1];
  if (!last) return insertModel(doc, 0, copilotModel(item));
  if (last.text === '' && !last.cells) {
    const { kind, text } = copilotLine(item);
    const a = insertText(doc, last.start, LEAD + text);
    const b = setLineKind(doc, last.start, last.start, kind);
    const c = setLink(doc, last.start, last.start + cpLen(CHIP), byUrl('copilot'));
    return { inverse: [...a.inverse, ...b.inverse, ...c.inverse], caret: last.start + cpLen(LEAD + text) };
  }
  return insertModel(doc, last.end + 1, copilotModel(item));
}
