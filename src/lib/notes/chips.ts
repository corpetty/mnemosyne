// Chips (phase 11): a link whose URL is `mnemosyne:<kind>/...` and that the editor draws as a
// pill instead of a hyperlink. The URL is the whole payload, so a chip travels as an ordinary
// link mark (markdown `[text](mnemosyne:...)`) and needs no model change. No DOM here.
//
//   mnemosyne:t/<part>/<seconds>     a moment in the recording (part index, whole seconds)
//   mnemosyne:person/<name>          a person, the name percent-encoded
//   mnemosyne:meeting/<id>           another meeting, its hex id
import { insertText, setLink, type CommandResult } from './commands.js';
import { cpLen, cpSlice, type Doc } from './model.js';

export type Chip =
  | { kind: 'time'; part: number; seconds: number }
  | { kind: 'person'; name: string }
  | { kind: 'meeting'; id: string };

const TIME = /^mnemosyne:t\/(\d+)\/(\d+)$/;
const PERSON = /^mnemosyne:person\/([^/]+)$/;
const MEETING = /^mnemosyne:meeting\/([0-9a-f]{8,32})$/;

/** Whole seconds only; a negative or non-finite value is clamped to 0. */
export function timeUrl(part: number, seconds: number): string {
  const whole = (n: number) => (Number.isFinite(n) ? Math.max(0, Math.floor(n)) : 0);
  return `mnemosyne:t/${whole(part)}/${whole(seconds)}`;
}

export function personUrl(name: string): string {
  return `mnemosyne:person/${encodeURIComponent(name)}`;
}

export function meetingUrl(id: string): string {
  return `mnemosyne:meeting/${id}`;
}

/** The chip a URL names, or null for anything that is not exactly one of the three forms. */
export function parseChip(url: string): Chip | null {
  const t = TIME.exec(url);
  if (t) {
    const part = Number(t[1]);
    const seconds = Number(t[2]);
    return Number.isSafeInteger(part) && Number.isSafeInteger(seconds) ? { kind: 'time', part, seconds } : null;
  }
  const p = PERSON.exec(url);
  if (p) {
    try {
      const name = decodeURIComponent(p[1]);
      return name ? { kind: 'person', name } : null;
    } catch {
      return null; // a malformed percent escape
    }
  }
  const m = MEETING.exec(url);
  return m ? { kind: 'meeting', id: m[1] } : null;
}

export function isChipUrl(url: string): boolean {
  return parseChip(url) !== null;
}

/** `m:ss`, or `h:mm:ss` from an hour on. */
export function formatClock(seconds: number): string {
  const total = Number.isFinite(seconds) ? Math.max(0, Math.floor(seconds)) : 0;
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const ss = String(s).padStart(2, '0');
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}

/**
 * Insert `text` at `at` as a chip: a closed link mark over the text, and one space after it
 * unless the next character already is one (typing then continues after the chip; a line end
 * gets the space too). Line breaks and cell separators in `text` become spaces. The caret goes
 * after the chip's space (the existing one when none was added). One CommandResult, so one
 * undo entry.
 */
export function insertChip(doc: Doc, at: number, text: string, url: string): CommandResult {
  const label = text.replace(/[\n\u001f]/g, ' ');
  const n = cpLen(label);
  if (!n) return { inverse: [], caret: at };
  const next = cpSlice(doc.web.text(doc.obj), at, at + 1);
  const space = next === ' ' ? '' : ' ';
  const typed = insertText(doc, at, label + space);
  const linked = setLink(doc, at, at + n, url);
  return { inverse: [...typed.inverse, ...linked.inverse], caret: at + n + 1 };
}
