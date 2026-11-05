// Readers' comments drawn in jsdom: the column (ReaderComments.svelte) on a real ReaderComments
// model with a stand-in sender, and the affordances Meeting.svelte draws only when comments are open.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { hex } from '$lib/sealed/envelope';
import { NOT_SENT_MS, ReaderComments, ReaderIdentity, storageKeys, type Draft, type KeyValue, type ReaderOp } from './comments';
import type { Manifest } from './format';
import Meeting from './Meeting.svelte';
import { reactive } from './reactive.svelte';
import Column from './ReaderComments.svelte';
import vec from '../../backend/tests/vectors/publication_v1.json';

const manifest: Manifest = JSON.parse(vec.manifest.json);
const PUB = manifest.id;
const KEY = new Uint8Array(32).fill(7);

class Store implements KeyValue {
	map = new Map<string, string>();
	getItem(k: string) {
		return this.map.get(k) ?? null;
	}
	setItem(k: string, v: string) {
		this.map.set(k, v);
	}
}

let target: HTMLElement;
let component: ReturnType<typeof mount> | null = null;

afterEach(() => {
	if (component) unmount(component);
	component = null;
	target?.remove();
	vi.useRealTimers();
});

function place(C: typeof Column | typeof Meeting, props: Record<string, unknown>) {
	target = document.createElement('div');
	document.body.appendChild(target);
	component = mount(C as typeof Column, { target, props: props as never });
	flushSync();
}

const comment = (id: string, over: Record<string, unknown> = {}) => ({
	id,
	parent_id: null,
	author: 'Bo (reader)',
	rid: 'feedfacefeedface',
	text: `text ${id}`,
	anchor: { target: 'decision', target_id: manifest.decisions[0].id, quote_id: null, quote: null },
	created_at: new Date(Date.now() - 5 * 60_000).toISOString(),
	updated_at: new Date(Date.now() - 5 * 60_000).toISOString(),
	resolved: false,
	deleted: false,
	detached: false,
	changed: false,
	...over
});

/** The column on a model whose changes bump `rev`, as Viewer.svelte wires it. */
function column(opts: { name?: string; key?: boolean; canAct?: boolean; threads?: unknown[]; draft?: Draft | null } = {}) {
	const store = new Store();
	const k = storageKeys(PUB);
	if (opts.name) store.setItem(k.name, opts.name);
	if (opts.key) store.setItem(k.key, hex(KEY));
	const sent: ReaderOp[] = [];
	const props = reactive({
		model: null as unknown as ReaderComments,
		rev: 0,
		manifest,
		canAct: opts.canAct ?? true,
		draft: opts.draft ?? (null as Draft | null),
		focus: null as { key: string; n: number } | null,
		ondraft: vi.fn((d: Draft | null) => (props.draft = d)),
		ongoto: vi.fn()
	});
	const model = new ReaderComments(new ReaderIdentity(PUB, () => store), (op) => sent.push(op), undefined, () => props.rev++);
	props.model = model;
	if (opts.threads) model.setThreads(opts.threads);
	place(Column, props);
	return { props, model, store, sent };
}

const q = (sel: string) => target.querySelector(sel);
const text = () => target.textContent!.replace(/\s+/g, ' ');
const button = (label: string) =>
	[...target.querySelectorAll('button')].find((b) => b.textContent?.replace(/\s+/g, ' ').trim().startsWith(label)) as HTMLButtonElement;

function type(el: HTMLTextAreaElement | HTMLInputElement, value: string) {
	el.value = value;
	el.dispatchEvent(new Event('input', { bubbles: true }));
	flushSync();
}

describe('the column', () => {
	it('asks for a name first, then shows the composer', () => {
		const { store, sent } = column({ canAct: true });
		expect(q('[data-testid="reader-name"]')).not.toBeNull();
		expect(text()).toContain('Your name, shown with your comments');
		expect(q('textarea')).toBeNull();
		const input = q('[data-testid="reader-name"] input') as HTMLInputElement;
		expect(input.maxLength).toBe(80);
		expect(button('Continue').disabled).toBe(true);
		type(input, '  Ana  ');
		expect(button('Continue').disabled).toBe(false);
		(q('[data-testid="reader-name"]') as HTMLFormElement).requestSubmit();
		flushSync();
		expect(store.getItem(storageKeys(PUB).name)).toBe('Ana');
		expect(q('[data-testid="reader-name"]')).toBeNull();
		const box = q('[data-testid="reader-compose"] textarea') as HTMLTextAreaElement;
		expect(box.placeholder).toBe('Comment on this meeting');
		expect(box.rows).toBe(2);
		expect(sent).toEqual([]);
	});

	it('sends on the meeting, shows it at once as "Sending…", then "Not sent · Retry" after 15 s', () => {
		vi.useFakeTimers();
		const { sent, model } = column({ name: 'Ana', key: true });
		const box = q('[data-testid="reader-compose"] textarea') as HTMLTextAreaElement;
		expect(button('Send').disabled).toBe(true);
		type(box, 'Overall good');
		button('Send').click();
		flushSync();
		expect(sent).toHaveLength(1);
		expect(sent[0]).toMatchObject({ op: 'create', text: 'Overall good', name: 'Ana', anchor: { target: '' } });
		expect(box.value).toBe('');
		expect(text()).toContain('Ana (reader)');
		expect(text()).toContain('Overall good');
		expect(q('[data-testid="comment-sending"]')).not.toBeNull();
		vi.advanceTimersByTime(NOT_SENT_MS + 20);
		flushSync();
		expect(q('[data-testid="comment-sending"]')).toBeNull();
		const late = q('[data-testid="comment-not-sent"]')!;
		expect(late.textContent!.replace(/\s+/g, ' ').trim()).toBe('Not sent · Retry');
		expect(late.className).toContain('text-amber-300');
		button('Retry').click();
		flushSync();
		expect(sent).toHaveLength(2);
		expect(sent[1]).toBe(sent[0]);
		expect(q('[data-testid="comment-sending"]')).not.toBeNull();
		// Confirmed by a THREADS that has it.
		model.setThreads([{ root: comment(sent[0].id, { text: 'Overall good', rid: model.rid, author: 'Ana (reader)' }), replies: [] }]);
		flushSync();
		expect(q('[data-testid="comment-sending"]')).toBeNull();
		expect(q('[data-testid="comment-not-sent"]')).toBeNull();
	});

	it('anchors the composer to a part with its "On:" line, and Cancel goes back to the meeting', () => {
		const d = manifest.decisions[0];
		const { props, sent } = column({ name: 'Ana', draft: { anchor: { target: 'decision', target_id: d.id, quote_id: null, exact: null, prefix: null, suffix: null }, line: d.text } });
		expect(q('[data-testid="compose-anchor"]')!.textContent).toBe(`On: ${d.text}`);
		type(q('[data-testid="reader-compose"] textarea') as HTMLTextAreaElement, 'Why?');
		button('Send').click();
		flushSync();
		expect(sent[0].anchor).toMatchObject({ target: 'decision', target_id: d.id });
		expect(props.ondraft).toHaveBeenLastCalledWith(null);
		props.draft = { anchor: { target: 'item', target_id: 'x', quote_id: null, exact: null, prefix: null, suffix: null }, line: 'An item' };
		flushSync();
		button('Cancel').click();
		flushSync();
		expect(q('[data-testid="compose-anchor"]')).toBeNull();
	});

	it('counts from 3,500 characters and disables Send past 4,000', () => {
		column({ name: 'Ana' });
		const box = q('[data-testid="reader-compose"] textarea') as HTMLTextAreaElement;
		type(box, 'x'.repeat(3499));
		expect(q('[data-testid="comment-counter"]')).toBeNull();
		type(box, 'x'.repeat(3500));
		expect(q('[data-testid="comment-counter"]')!.textContent).toBe('3500 / 4000');
		expect(button('Send').disabled).toBe(false);
		type(box, 'x'.repeat(4001));
		expect(button('Send').disabled).toBe(true);
	});

	it('shows each thread\'s anchor, Reply for all, Edit and Delete only on my own, "removed", and collapses resolved ones', async () => {
		const { model, props } = column({
			name: 'Ana',
			key: true,
			threads: [
				{ root: comment('aaaaaaaaaaaa'), replies: [comment('bbbbbbbbbbbb', { parent_id: 'aaaaaaaaaaaa', author: 'Corey', rid: null, text: 'owner reply' })] },
				{ root: comment('cccccccccccc', { anchor: { target: '', target_id: null, quote_id: null, quote: null }, deleted: true, text: '' }), replies: [] },
				{ root: comment('dddddddddddd', { resolved: true, text: 'old one' }), replies: [comment('eeeeeeeeeeee', { parent_id: 'dddddddddddd' })] }
			]
		});
		await model.init();
		const mine = model.rid!;
		model.setThreads([
			{ root: comment('aaaaaaaaaaaa'), replies: [comment('bbbbbbbbbbbb', { parent_id: 'aaaaaaaaaaaa', author: 'Corey', rid: null, text: 'owner reply' })] },
			{ root: comment('ffffffffffff', { rid: mine, author: 'Ana (reader)', text: 'mine', anchor: { target: '', target_id: null, quote_id: null, quote: null } }), replies: [] },
			{ root: comment('cccccccccccc', { anchor: { target: '', target_id: null, quote_id: null, quote: null }, deleted: true, text: '' }), replies: [] },
			{ root: comment('dddddddddddd', { resolved: true, text: 'old one' }), replies: [comment('eeeeeeeeeeee', { parent_id: 'dddddddddddd' })] }
		]);
		flushSync();
		const anchor = [...target.querySelectorAll('[data-testid="thread-anchor"]')].map((e) => e.textContent);
		expect(anchor).toEqual([`On: ${manifest.decisions[0].text}`]);
		(target.querySelector('[data-testid="thread-anchor"]') as HTMLButtonElement).click();
		expect(props.ongoto).toHaveBeenCalledWith(`decision:${manifest.decisions[0].id}`);
		const card = (id: string) => target.querySelector(`[data-thread="${id}"]`)!;
		const labels = (id: string) => [...card(id).querySelectorAll('button')].map((b) => b.textContent!.trim());
		expect(labels('aaaaaaaaaaaa')).toEqual(['On: ' + manifest.decisions[0].text, 'Reply']);
		expect(labels('ffffffffffff')).toEqual(['Reply', 'Edit', 'Delete']);
		expect(card('cccccccccccc').textContent).toContain('removed');
		expect(card('cccccccccccc').querySelector('.italic.text-gray-600')!.textContent).toBe('removed');
		expect(text()).not.toContain('old one');
		const row = target.querySelector('[data-testid="thread-resolved"]') as HTMLButtonElement;
		expect(row.textContent!.trim()).toBe('Resolved · 2 comments');
		row.click();
		flushSync();
		expect(text()).toContain('old one');
		expect(labels('dddddddddddd')).not.toContain('Reply');
	});

	it('edits and deletes my own comment through pending ops', async () => {
		const { model, sent } = column({ name: 'Ana', key: true });
		await model.init();
		model.setThreads([{ root: comment('ffffffffffff', { rid: model.rid, author: 'Ana (reader)', text: 'mine' }), replies: [] }]);
		flushSync();
		button('Edit').click();
		flushSync();
		const box = target.querySelector('textarea[aria-label="Edit your comment"]') as HTMLTextAreaElement;
		type(box, 'mine, better');
		button('Save').click();
		flushSync();
		expect(sent.at(-1)).toMatchObject({ op: 'edit', id: 'ffffffffffff', text: 'mine, better', anchor: null, parent_id: null });
		expect(text()).toContain('mine, better');
		expect(q('[data-testid="comment-sending"]')).not.toBeNull();
		model.setThreads([{ root: comment('ffffffffffff', { rid: model.rid, author: 'Ana (reader)', text: 'mine, better' }), replies: [] }]);
		flushSync();
		button('Delete').click();
		flushSync();
		button('Yes').click();
		flushSync();
		expect(sent.at(-1)).toMatchObject({ op: 'delete', id: 'ffffffffffff', text: null });
		expect(text()).toContain('removed');
		expect(q('[data-testid="comment-sending"]')).not.toBeNull();
	});

	it('read-only with comments off: threads shown, no composer, no Reply', () => {
		column({ name: 'Ana', canAct: false, threads: [{ root: comment('aaaaaaaaaaaa'), replies: [] }] });
		expect(q('[data-testid="reader-compose"]')).toBeNull();
		expect(q('[data-testid="reader-name"]')).toBeNull();
		expect(text()).toContain('text aaaaaaaaaaaa');
		expect(button('Reply')).toBeUndefined();
	});

	it('asks for a name before a first reply too', () => {
		column({ canAct: true, threads: [{ root: comment('aaaaaaaaaaaa'), replies: [] }] });
		button('Reply').click();
		flushSync();
		const card = target.querySelector('[data-thread="aaaaaaaaaaaa"]')!;
		expect(card.querySelector('[data-testid="reader-name"]')).not.toBeNull();
		expect(card.querySelector('textarea')).toBeNull();
	});
});

describe('affordances in the meeting', () => {
	it('there are none without `comments` (no live, comments off, the preview)', () => {
		place(Meeting, { manifest, loadClip: vi.fn() });
		expect(target.querySelectorAll('[data-testid="comment-on"]')).toHaveLength(0);
		expect(target.querySelectorAll('[data-testid="comment-count"]')).toHaveLength(0);
		expect(target.textContent).not.toContain('Comment');
	});

	it('puts "Comment" on each decision, question, action item and quote, and "n comments" where threads are', () => {
		const onComment = vi.fn();
		const onShow = vi.fn();
		const d = manifest.decisions[0];
		place(Meeting, { manifest, loadClip: vi.fn(), comments: { counts: { [`decision:${d.id}`]: 2 }, onComment, onShow } });
		const n = manifest.questions.length + manifest.action_items.length + manifest.quotes.length;
		const buttons = [...target.querySelectorAll<HTMLButtonElement>('[data-testid="comment-on"]')];
		expect(buttons).toHaveLength(n);
		for (const b of buttons) {
			expect(b.className).toContain('text-xs text-gray-500 hover:text-gray-300');
			expect(b.className).toContain('md:opacity-0');
		}
		const count = target.querySelector<HTMLButtonElement>('[data-testid="comment-count"]')!;
		expect(count.textContent).toBe('2 comments');
		expect(count.className).toContain('text-sky-400');
		count.click();
		expect(onShow).toHaveBeenCalledWith(`decision:${d.id}`);
		const quoteRow = target.querySelector(`[data-anchor="quote:${manifest.quotes[0].id}"]`)!;
		(quoteRow.querySelector('[data-testid="comment-on"]') as HTMLButtonElement).click();
		expect(onComment).toHaveBeenLastCalledWith({
			anchor: { target: 'quote', target_id: null, quote_id: manifest.quotes[0].id, exact: null, prefix: null, suffix: null },
			line: manifest.quotes[0].text
		});
		const item = manifest.action_items[0];
		(target.querySelector(`[data-anchor="item:${item.id}"] [data-testid="comment-on"]`) as HTMLButtonElement).click();
		expect(onComment).toHaveBeenLastCalledWith({
			anchor: { target: 'item', target_id: item.id, quote_id: null, exact: null, prefix: null, suffix: null },
			line: item.text
		});
	});

	it('tints the part a thread pointed at', () => {
		const d = manifest.decisions[0];
		place(Meeting, { manifest, loadClip: vi.fn(), highlight: `decision:${d.id}` });
		const row = target.querySelector(`[data-anchor="decision:${d.id}"]`)!;
		expect(row.className).toContain('bg-sky-950/40');
		expect(row.className).toContain('transition-colors');
		expect(target.querySelector('[data-anchor="body"]')!.className).not.toContain('bg-sky-950/40');
	});

	it('offers "Comment" over a passage selected in the summary, anchored by its text', () => {
		const onComment = vi.fn();
		place(Meeting, { manifest, loadClip: vi.fn(), comments: { counts: {}, onComment, onShow: vi.fn() } });
		const body = target.querySelector('[data-anchor="body"] > div')!;
		const node = [...body.querySelectorAll('p')].map((p) => p.firstChild).find((t) => t && t.nodeType === 3 && (t.textContent ?? '').length > 20)!;
		const range = document.createRange();
		range.setStart(node, 3);
		range.setEnd(node, 15);
		const sel = document.getSelection()!;
		sel.removeAllRanges();
		sel.addRange(range);
		document.dispatchEvent(new Event('selectionchange'));
		flushSync();
		const pick = target.querySelector<HTMLButtonElement>('[data-testid="comment-passage"]')!;
		expect(pick).not.toBeNull();
		expect(pick.className).toContain('rounded border border-gray-700 bg-gray-900 px-2 py-1 text-xs');
		pick.click();
		const full = body.textContent!;
		const exact = node.textContent!.slice(3, 15).trim();
		const draft = onComment.mock.calls[0][0] as Draft;
		expect(draft.anchor.target).toBe('body');
		expect(draft.anchor.exact).toBe(exact);
		const at = full.indexOf(draft.anchor.prefix! + exact);
		expect(at).toBeGreaterThanOrEqual(0);
		expect(draft.anchor.prefix!.length).toBeLessThanOrEqual(32);
		expect(draft.anchor.suffix!.length).toBeLessThanOrEqual(32);
		expect(full.slice(at + draft.anchor.prefix!.length + exact.length).startsWith(draft.anchor.suffix!)).toBe(true);
		expect(draft.line).toBe(exact);
	});
});
