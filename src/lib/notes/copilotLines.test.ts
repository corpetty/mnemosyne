import { describe, expect, it } from 'vitest';
import { ORIGIN, WasmHashWeb, view } from './test-harness.js';
import { appendToDoc, appendToText, copilotLine, copilotMarkdown, copilotModel, type CopilotItem } from './copilotLines.js';
import { insertModel, insertText, setLineKind } from './commands.js';
import { ensureTerminated, lines, openDoc } from './model.js';

const mk = () => openDoc(new WasmHashWeb(), ORIGIN);
const plain = (d: ReturnType<typeof mk>) => d.web.text(d.obj);

describe('copilotLine / copilotModel', () => {
  it('a summary point is a bullet', () => {
    expect(copilotModel({ kind: 'summary', text: 'We reviewed Q3' })).toEqual({
      text: 'We reviewed Q3\n',
      lines: [{ kind: 'bullet' }],
      inline: []
    });
  });

  it('a decision is a bullet "Decided: ..."', () => {
    expect(copilotLine({ kind: 'decision', text: 'Ship on Friday' })).toEqual({ kind: 'bullet', text: 'Decided: Ship on Friday' });
  });

  it('an action item is a task, with the owner in brackets when there is one', () => {
    expect(copilotLine({ kind: 'action', text: 'Send the deck', owner: 'Ana' })).toEqual({ kind: 'task', text: 'Send the deck (Ana)' });
    expect(copilotLine({ kind: 'action', text: 'Send the deck', owner: null })).toEqual({ kind: 'task', text: 'Send the deck' });
    expect(copilotLine({ kind: 'action', text: 'Send the deck', owner: '  ' })).toEqual({ kind: 'task', text: 'Send the deck' });
  });

  it('an open question is a bullet "Question: ..."', () => {
    expect(copilotLine({ kind: 'question', text: 'Who pays?' })).toEqual({ kind: 'bullet', text: 'Question: Who pays?' });
  });

  it('collapses newlines and spaces so the item stays one line', () => {
    expect(copilotModel({ kind: 'summary', text: ' a\n b   c ' }).text).toBe('a b c\n');
  });
});

describe('markdown for the plain-text notes', () => {
  it('writes bullets and tasks', () => {
    expect(copilotMarkdown({ kind: 'decision', text: 'X' })).toBe('- Decided: X');
    expect(copilotMarkdown({ kind: 'action', text: 'Y', owner: 'Bo' })).toBe('- [ ] Y (Bo)');
  });

  it('appends after existing notes with or without a final newline', () => {
    const item: CopilotItem = { kind: 'summary', text: 'Z' };
    expect(appendToText('', item)).toBe('- Z\n');
    expect(appendToText('  \n', item)).toBe('- Z\n');
    expect(appendToText('hello', item)).toBe('hello\n- Z\n');
    expect(appendToText('hello\n', item)).toBe('hello\n- Z\n');
  });
});

describe('appendToDoc', () => {
  it('an empty document gets the line as its first', () => {
    const d = mk();
    appendToDoc(d, { kind: 'action', text: 'Call Bo', owner: 'Ana' });
    expect(plain(d)).toBe('Call Bo (Ana)\n');
    expect(view(lines(d))).toContain('task');
    expect(lines(d)).toHaveLength(1);
    expect(lines(d)[0].kind).toBe('task');
  });

  it('goes after a non-empty last line', () => {
    const d = mk();
    ensureTerminated(d);
    insertText(d, 0, 'Agenda');
    appendToDoc(d, { kind: 'summary', text: 'first' });
    appendToDoc(d, { kind: 'question', text: 'second?' });
    expect(plain(d)).toBe('Agenda\nfirst\nQuestion: second?\n');
    expect(lines(d).map((l) => l.kind)).toEqual([null, 'bullet', 'bullet']);
  });

  it('fills an empty last line rather than leaving a gap, whatever kind it had', () => {
    const d = mk();
    insertModel(d, 0, { text: 'Title\n\n', lines: [{ kind: 'h1' }, { kind: 'bullet' }], inline: [] });
    expect(lines(d).map((l) => l.text)).toEqual(['Title', '']);
    appendToDoc(d, { kind: 'decision', text: 'go' });
    expect(plain(d)).toBe('Title\nDecided: go\n');
    expect(lines(d).map((l) => l.kind)).toEqual(['h1', 'bullet']);
    setLineKind(d, 6, 6, 'h2');
    appendToDoc(d, { kind: 'summary', text: 'more' });
    expect(plain(d)).toBe('Title\nDecided: go\nmore\n');
  });

  it('does not format the new line like the bold text before it', () => {
    const d = mk();
    insertModel(d, 0, { text: 'bold\n', lines: [{ kind: null }], inline: [{ from: 0, to: 4, kind: 'bold' }] });
    appendToDoc(d, { kind: 'summary', text: 'plain' });
    expect(lines(d)[1].runs.every((r) => !r.bold)).toBe(true);
  });
});
