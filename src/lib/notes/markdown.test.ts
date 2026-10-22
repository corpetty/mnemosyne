// notes-core's markdown through the wasm: parse -> insertModel -> lines() -> NotesModel ->
// renderMarkdown must give back the canonical markdown. The Rust side tests the parser and
// renderer (notes-core/tests); this checks the JSON the client sees and that commands.ts
// applies every construct.
import { describe, expect, it } from 'vitest';
import { single } from './test-harness.js';
import { insertModel, type NotesModel } from './commands.js';
import { CELL_SEP, cpLen, encodeAlign, lines, type Line } from './model.js';
import { parseMarkdown, renderMarkdown } from './pkg/notes_wasm.js';

const SAMPLE = [
  '# Title',
  '## Sub',
  '### Third',
  '',
  'Plain with **bold**, *italic*, ~~gone~~, `code` and [a link](https://example.com/a/very/long/url?q=1).',
  '**bold *and italic*** and ***both***',
  '',
  '- bullet',
  '1. numbered',
  '- [ ] open task',
  '- [x] done task',
  '',
  '> quoted',
  '',
  '```py',
  'print("hi")',
  '',
  'x = 1',
  '```',
  '',
  '| Left | Center | Right | None |',
  '|:-----|:------:|------:|------|',
  '| a | **b** | `c` | [d](https://e.example) |',
  '| ünï | 日本語 | 😀 | |',
  '',
  'Last line, with émoji 😀 and **bold 😀**.',
  ''
].join('\n');

const parse = (md: string): NotesModel => JSON.parse(parseMarkdown(md));

/** The document's lines as a NotesModel (what the editor would put on the clipboard). */
function modelOf(ls: readonly Line[]): NotesModel {
  const model: NotesModel = { text: '', lines: [], inline: [] };
  let at = 0;
  for (const l of ls) {
    let off = at;
    for (const r of l.runs) {
      const n = cpLen(r.text);
      for (const kind of ['bold', 'italic', 'strike', 'code'] as const) {
        if (r[kind]) model.inline.push({ from: off, to: off + n, kind, value: 'true' });
      }
      if (r.link) model.inline.push({ from: off, to: off + n, kind: 'link', value: r.link });
      off += n;
    }
    model.text += l.text + '\n';
    model.lines.push({
      kind: l.kind,
      ...(l.lang ? { lang: l.lang } : {}),
      ...(l.align ? { align: l.align } : {})
    });
    at += cpLen(l.text) + 1;
  }
  return model;
}

/** Equal for our purposes: same text and lines, inline ranges merged per kind and value. */
function normalized(m: NotesModel) {
  const cover = new Map<string, Set<number>>();
  for (const r of m.inline) {
    const key = `${r.kind}=${r.kind === 'link' ? r.value : ''}`;
    const set = cover.get(key) ?? new Set<number>();
    for (let i = r.from; i < r.to; i++) set.add(i);
    cover.set(key, set);
  }
  return {
    text: m.text,
    lines: m.lines.map((l) => ({ kind: l.kind, lang: l.lang ?? null, align: l.align?.some((a) => a) ? encodeAlign(l.align) : null })),
    cover: [...cover].map(([k, v]) => [k, [...v].sort((a, b) => a - b)]).sort()
  };
}

describe('parseMarkdown', () => {
  const model = parse(SAMPLE);

  it('gives the NotesModel shape', () => {
    expect(typeof model.text).toBe('string');
    expect(model.text.endsWith('\n')).toBe(true);
    expect(model.lines).toHaveLength(model.text.split('\n').length - 1);
    const kinds = new Set(model.lines.map((l) => l.kind));
    for (const k of ['h1', 'h2', 'h3', 'bullet', 'number', 'task', 'task_done', 'quote', 'code', 'table', 'table_head', null]) {
      expect(kinds.has(k as never), String(k)).toBe(true);
    }
    expect(new Set(model.inline.map((m) => m.kind))).toEqual(new Set(['bold', 'italic', 'strike', 'code', 'link']));
    for (const m of model.inline) {
      expect(m.from).toBeLessThan(m.to);
      expect(m.to).toBeLessThanOrEqual(cpLen(model.text));
      if (m.kind === 'link') expect(m.value).toMatch(/^https:\/\//);
      else expect(m.value).toBe('true');
    }
    expect(model.lines.find((l) => l.kind === 'code' && l.lang)?.lang).toBe('py');
    const head = model.lines.find((l) => l.kind === 'table_head');
    expect(head?.align).toEqual(['left', 'center', 'right', null]);
    expect(model.text).toContain(`Left${CELL_SEP}Center${CELL_SEP}Right${CELL_SEP}None`);
  });

  it('offsets are code points', () => {
    const m = parse('😀 **x**\n');
    expect(m.text).toBe('😀 x\n');
    expect(m.inline).toEqual([{ from: 2, to: 3, kind: 'bold', value: 'true' }]);
  });
});

describe('markdown through the document', () => {
  it('applies every construct and renders back to the same markdown', () => {
    const model = parse(SAMPLE);
    const A = single();
    insertModel(A.doc, 0, model);
    const ls = lines(A.doc);

    // What the document holds is the model.
    expect(A.text()).toBe(model.text);
    expect(normalized(modelOf(ls))).toEqual(normalized(model));
    expect(ls.some((l) => l.cells && l.cells.length === 4)).toBe(true);
    const table = ls.filter((l) => l.kind === 'table_head' || l.kind === 'table');
    expect(table).toHaveLength(3);
    expect(table[0].align).toEqual(['left', 'center', 'right', null]);
    expect(table[2].cells?.map((c) => c.map((r) => r.text).join(''))).toEqual(['ünï', '日本語', '😀', '']);
    expect(ls.find((l) => l.kind === 'code')?.lang).toBe('py');

    // And its markdown is the canonical markdown of the input, a fixpoint.
    const canonical = renderMarkdown(JSON.stringify(model));
    expect(renderMarkdown(JSON.stringify(modelOf(ls)))).toBe(canonical);
    expect(renderMarkdown(parseMarkdown(canonical))).toBe(canonical);
    expect(canonical).toContain('| Left | Center | Right | None |');
    expect(canonical).toContain('```py\nprint("hi")');
  });

  it('splits the host line when pasted mid-line (the model ends in a newline)', () => {
    const A = single();
    insertModel(A.doc, 0, parse('before after\n'));
    insertModel(A.doc, 7, parse('**bold** '));
    const ls = lines(A.doc);
    expect(ls.map((l) => l.text)).toEqual(['before bold ', 'after']);
    expect(ls[0].runs.filter((r) => r.bold).map((r) => r.text)).toEqual(['bold']);
  });

  it('renders a selection (no trailing newline) and rejects non-models', () => {
    expect(renderMarkdown(JSON.stringify({ text: 'a b', lines: [], inline: [{ from: 2, to: 3, kind: 'bold' }] }))).toBe(
      'a **b**\n'
    );
    expect(() => renderMarkdown('nope')).toThrow(/invalid model/);
    expect(() => renderMarkdown(JSON.stringify({ text: 'a', inline: [{ from: 0, to: 9, kind: 'bold' }] }))).toThrow(/outside/);
  });
});
