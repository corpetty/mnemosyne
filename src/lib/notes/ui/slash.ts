// The `/` menu and the `@` / `[[` pickers (phase 11, task 4): the pure parts. When a typed
// character opens a menu (`triggerAt`), where the menu's query is now (`regionOf`, `queryOf`),
// what the menu lists (`menuItems`), how a query narrows it (`filter`) and where the listbox goes
// (`placeMenu`). No DOM here; SlashMenu.svelte drives it.
//
// The trigger is tracked by its character's element id (seqIdAt / seqPositionOf), never by an
// offset: a remote insert before it moves the offset, not the id, so the query stays the text
// between the trigger and the caret.
import { CELL_SEP, cpLen, cpSlice, lineAt, lines, spansOf, valuesAt, type Doc, type LineKind } from '../model.js';
import type { Rect } from './placement.js';

/** A query longer than this is not a menu query any more (someone typed on after a `/`). */
export const MAX_QUERY = 40;

export type Trigger = '/' | '@' | '[[';

/** The trigger a typed character completes: `[` only as the second one of `[[`. */
export function triggerOf(char: string): Trigger | null {
  return char === '/' ? '/' : char === '@' ? '@' : char === '[' ? '[[' : null;
}

/**
 * Does the character just typed (`char`, the caret right after it) open a menu? `/`, `@` or the
 * second `[` of `[[`, when the trigger starts a line or follows white space, and not in a code
 * line, a table line, inline code or a link (chips are links).
 */
export function triggerAt(doc: Doc, caret: number, char: string): boolean {
  const trigger = triggerOf(char);
  if (!trigger) return false;
  const at = caret - trigger.length;
  if (at < 0) return false;
  const text = doc.web.text(doc.obj);
  if (cpSlice(text, at, caret) !== trigger) return false;
  const line = lineAt(lines(doc), at);
  if (line.cells || line.kind === 'code' || line.kind === 'table' || line.kind === 'table_head') return false;
  if (at > line.start) {
    const prev = cpSlice(text, at - 1, at);
    // White space, U+00A0 included (\s covers it): an engine may keep a trailing space as one.
    if (prev === CELL_SEP || !/^\s$/u.test(prev)) return false;
  }
  const spans = spansOf(doc);
  const has = (pos: number, kind: string) => valuesAt(spans, pos, kind);
  for (let i = at; i < caret; i++) if (has(i, 'code').length || has(i, 'link').length) return false;
  // Between two characters of the same code span or link (a space inside `a b`, a linked phrase).
  if (at > 0 && caret < cpLen(text)) {
    if (has(at - 1, 'code').length && has(caret, 'code').length) return false;
    const before = has(at - 1, 'link');
    const after = has(caret, 'link');
    if (before.some((u) => after.includes(u))) return false;
  }
  return true;
}

/**
 * Where a menu's query starts. With a trigger: `id` is the trigger's first character and the query
 * follows the trigger. Without one (Ctrl+/, the Insert button, a picker opened from the menu):
 * the query follows element `id` (null = the start of the document).
 */
export interface Anchor {
  id: string | null;
  trigger: Trigger | '';
}

/** An open menu's anchor plus what a pick deletes: from element `cut` (null = from the query). */
export interface MenuSpan {
  anchor: Anchor;
  cut: string | null;
}

export interface Region {
  /** What a pick deletes: [from, to). */
  from: number;
  to: number;
  /** Where the query starts. */
  start: number;
  query: string;
}

/** Where the query starts now, or null when the anchor is gone or no longer what it was. */
function queryStart(doc: Doc, text: string, anchor: Anchor): number | null {
  if (anchor.trigger) {
    if (anchor.id === null) return null;
    const pos = doc.web.seqPositionOf(doc.obj, anchor.id);
    if (pos === undefined) return null;
    const len = anchor.trigger.length;
    return cpSlice(text, pos, pos + len) === anchor.trigger ? pos + len : null;
  }
  if (anchor.id === null) return 0;
  const pos = doc.web.seqPositionOf(doc.obj, anchor.id);
  return pos === undefined ? null : pos + 1;
}

const breaks = (s: string) => s.includes('\n') || s.includes(CELL_SEP);

/**
 * The open menu's region with the caret at `caret`: null when the trigger (or `cut`) is gone, the
 * caret moved before the query or off its line, or the query grew past MAX_QUERY.
 */
export function regionOf(doc: Doc, span: MenuSpan, caret: number): Region | null {
  const text = doc.web.text(doc.obj);
  const start = queryStart(doc, text, span.anchor);
  if (start === null || caret < start) return null;
  const query = cpSlice(text, start, caret);
  if (breaks(query) || cpLen(query) > MAX_QUERY) return null;
  let from = span.anchor.trigger ? start - span.anchor.trigger.length : start;
  if (span.cut !== null) {
    const c = doc.web.seqPositionOf(doc.obj, span.cut);
    if (c === undefined || c > from || breaks(cpSlice(text, c, from))) return null;
    from = c;
  }
  return { from, to: caret, start, query };
}

/**
 * The text between the trigger (its first character's element id) and the caret, or null when
 * the trigger is gone, the caret left its line or moved before it, or the query got too long.
 */
export function queryOf(doc: Doc, triggerId: string, caret: number, trigger: Trigger = '/'): string | null {
  return regionOf(doc, { anchor: { id: triggerId, trigger }, cut: null }, caret)?.query ?? null;
}

// ---- the menu's items --------------------------------------------------------------------------

export type MenuAction =
  | { kind: 'time' }
  | { kind: 'action' }
  | { kind: 'person' }
  | { kind: 'meeting' }
  | { kind: 'line'; line: LineKind | null }
  | { kind: 'table' };

export type MenuGroup = 'meeting' | 'blocks';

export const GROUP_LABEL: Record<MenuGroup, string> = { meeting: 'This meeting', blocks: 'Blocks' };

export interface MenuItem {
  /** Stable: the option's DOM id is made from it. */
  key: string;
  name: string;
  aliases: readonly string[];
  group: MenuGroup;
  /** Shown after the name (the time's label). */
  hint?: string;
  act: MenuAction;
}

const BLOCKS: MenuItem[] = [
  { key: 'text', name: 'Text', aliases: ['plain', 'paragraph', 'normal'], group: 'blocks', act: { kind: 'line', line: null } },
  { key: 'h1', name: 'Heading 1', aliases: ['h1', 'title'], group: 'blocks', act: { kind: 'line', line: 'h1' } },
  { key: 'h2', name: 'Heading 2', aliases: ['h2', 'subtitle'], group: 'blocks', act: { kind: 'line', line: 'h2' } },
  { key: 'h3', name: 'Heading 3', aliases: ['h3'], group: 'blocks', act: { kind: 'line', line: 'h3' } },
  { key: 'bullet', name: 'Bulleted list', aliases: ['bullet', 'list', 'ul', '-'], group: 'blocks', act: { kind: 'line', line: 'bullet' } },
  { key: 'number', name: 'Numbered list', aliases: ['numbered', 'ol', '1.'], group: 'blocks', act: { kind: 'line', line: 'number' } },
  { key: 'task', name: 'Task list', aliases: ['todo', 'check', 'checkbox', '[]'], group: 'blocks', act: { kind: 'line', line: 'task' } },
  { key: 'quote', name: 'Quote', aliases: ['blockquote', '>'], group: 'blocks', act: { kind: 'line', line: 'quote' } },
  { key: 'code', name: 'Code block', aliases: ['code', 'pre', '```'], group: 'blocks', act: { kind: 'line', line: 'code' } },
  { key: 'table', name: 'Table', aliases: ['table', 'grid'], group: 'blocks', act: { kind: 'table' } }
];

export interface MenuContext {
  /** `inserts.time()`'s label, null when there is no moment to insert. */
  time: string | null;
  people: boolean;
  meetings: boolean;
  /** The caret's line: chips do not go into code, line kinds do not apply to a table row. */
  line: 'text' | 'code' | 'table';
}

/** What the `/` menu lists, "This meeting" first, then "Blocks". */
export function menuItems(c: MenuContext): MenuItem[] {
  const out: MenuItem[] = [];
  if (c.line !== 'code') {
    if (c.time !== null)
      out.push({ key: 'time', name: 'Time now', aliases: ['time', 'now', 'timestamp', 'clock'], group: 'meeting', hint: c.time, act: { kind: 'time' } });
    if (c.line === 'text')
      out.push({ key: 'action', name: 'Action item', aliases: ['action', 'assign', 'owner'], group: 'meeting', act: { kind: 'action' } });
    if (c.people) out.push({ key: 'person', name: 'Person', aliases: ['person', 'mention', 'people', '@'], group: 'meeting', act: { kind: 'person' } });
    if (c.meetings)
      out.push({ key: 'meeting', name: 'Earlier meeting', aliases: ['meeting', 'link', '[['], group: 'meeting', act: { kind: 'meeting' } });
  }
  if (c.line !== 'table') out.push(...BLOCKS);
  return out;
}

/**
 * The items a query keeps, by name and aliases, ignoring case: those where one of them starts
 * with the query first, then those containing it; each in the given order. An empty query keeps
 * everything.
 */
export function filter<T extends { name: string; aliases?: readonly string[] }>(items: readonly T[], query: string): T[] {
  const q = query.toLowerCase();
  if (!q) return [...items];
  const prefix: T[] = [];
  const inside: T[] = [];
  for (const it of items) {
    const words = [it.name, ...(it.aliases ?? [])].map((w) => w.toLowerCase());
    if (words.some((w) => w.startsWith(q))) prefix.push(it);
    else if (words.some((w) => w.includes(q))) inside.push(it);
  }
  return [...prefix, ...inside];
}

/** A string safe inside a DOM id (an option's, from a person's name or a meeting id). */
export function idPart(s: string): string {
  return s.replace(/[^A-Za-z0-9_-]/gu, (c) => `_${c.codePointAt(0)!.toString(16)}`);
}

// ---- where the listbox goes ------------------------------------------------------------------------

export interface MenuPlacementInput {
  /** The caret's client rect. */
  caret: Rect;
  /** The editor box's client rect (inside its border): the coordinate origin. */
  box: Rect;
  scrollTop?: number;
  scrollLeft?: number;
  menu: { width: number; height: number };
  /** The lowest client y the menu may reach before it flips above the line (the viewport, or a scrolling box). */
  limitBottom: number;
  /** The highest client y it may reach above. */
  limitTop?: number;
  gap?: number;
}

/**
 * Below the caret's line, flipped above when it would pass `limitBottom` and fits above; left
 * aligned with the caret and kept inside the box. In the box's coordinates, scroll included.
 */
export function placeMenu(p: MenuPlacementInput): { top: number; left: number; side: 'above' | 'below' } {
  const gap = p.gap ?? 4;
  const st = p.scrollTop ?? 0;
  const sl = p.scrollLeft ?? 0;
  const below = p.caret.bottom + gap;
  const above = p.caret.top - gap - p.menu.height;
  const flip = below + p.menu.height > p.limitBottom && above >= (p.limitTop ?? 0);
  const top = (flip ? above : below) - p.box.top + st;
  const width = p.box.right - p.box.left;
  const maxLeft = Math.max(0, width - p.menu.width);
  const left = Math.min(Math.max(0, p.caret.left - p.box.left), maxLeft) + sl;
  return { top, left, side: flip ? 'above' : 'below' };
}
