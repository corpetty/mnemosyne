// The comments panel and the agenda in jsdom, on a stand-in for GuestDoc.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Agenda from './Agenda.svelte';
import Comments from './Comments.svelte';
import type { GuestComment, GuestDoc, GuestThread } from './doc';
import { reactive } from './reactive.svelte';

function comment(id: string, over: Partial<GuestComment> = {}): GuestComment {
	return {
		id,
		parent_id: null,
		author: 'Olga',
		gid: '',
		text: `text ${id}`,
		quote: 'the plan',
		mark: `comment:${id}`,
		created_at: new Date(Date.now() - 5 * 60_000).toISOString(),
		updated_at: new Date(Date.now() - 5 * 60_000).toISOString(),
		resolved: false,
		deleted: false,
		detached: false,
		changed: false,
		...over
	};
}
const thread = (id: string, over: Partial<GuestComment> = {}, replies: GuestComment[] = []): GuestThread => ({
	root: comment(id, over),
	replies
});

class FakeGuest {
	threads: GuestThread[] = [];
	you = { gid: 'me', name: 'Ada' };
	catchUps = 1;
	sendComment = vi.fn(async (_op: Record<string, unknown>) => true);
	synced = vi.fn(async () => {});
}

let target: HTMLElement;
let component: ReturnType<typeof mount> | null = null;

function render(C: typeof Comments | typeof Agenda, props: Record<string, unknown>) {
	target = document.createElement('div');
	document.body.appendChild(target);
	component = mount(C as typeof Comments, { target, props: props as never });
	flushSync();
}

function panel(g: FakeGuest, over: Record<string, unknown> = {}) {
	const props = reactive({
		guest: g as unknown as GuestDoc,
		rev: 0,
		canAct: true,
		draft: null as null | { text: string; first: string; last: string },
		focus: null,
		ondone: vi.fn(),
		ongoto: vi.fn(),
		...over
	});
	render(Comments, props);
	return props;
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	target?.remove();
	vi.useRealTimers();
});

const button = (label: string) =>
	[...target.querySelectorAll('button')].find((b) => b.textContent?.trim().startsWith(label)) as HTMLButtonElement;

function type(el: HTMLTextAreaElement, value: string) {
	el.value = value;
	el.dispatchEvent(new Event('input', { bubbles: true }));
	flushSync();
}
function key(el: HTMLElement, k: string, shiftKey = false) {
	const e = new KeyboardEvent('keydown', { key: k, shiftKey, bubbles: true, cancelable: true });
	el.dispatchEvent(e);
	flushSync();
	return e;
}

describe('Agenda', () => {
	it('ticks covered items and leaves the others hollow', () => {
		render(Agenda, {
			items: [
				{ text: 'Budget', covered: true },
				{ text: 'Hiring', covered: false }
			]
		});
		expect(target.querySelector('h2')!.textContent).toBe('Agenda');
		const lis = target.querySelectorAll('li');
		expect(lis).toHaveLength(2);
		expect(lis[0].textContent).toContain('✓');
		expect(lis[0].className).toContain('text-gray-500');
		expect(lis[1].textContent).not.toContain('✓');
		expect(lis[1].querySelector('.rounded-full')).not.toBeNull();
	});

	it('renders nothing without items', () => {
		render(Agenda, { items: [] });
		expect(target.textContent?.trim()).toBe('');
		expect(target.querySelector('h2')).toBeNull();
	});
});

describe('Comments', () => {
	it('lists open threads with the open count, the quote, replies, and resolved ones collapsed', () => {
		const g = new FakeGuest();
		g.threads = [
			thread('t1', {}, [comment('r1', { parent_id: 't1', author: 'Bo (guest)', gid: 'bo', text: 'agreed' })]),
			thread('t2', { resolved: true, text: 'old question' }),
			thread('t3', { changed: true, quote: 'moved text' })
		];
		panel(g);
		expect(target.querySelector('h2')!.textContent).toBe('Comments');
		expect(target.querySelector('[data-testid="guest-comments-open"]')!.textContent).toBe('2 open');
		expect(target.textContent).toContain('“the plan”');
		expect(target.textContent).toContain('agreed');
		expect(target.textContent).toContain('(text changed)');
		expect(target.textContent).toContain('5 min ago');
		expect(target.textContent).not.toContain('old question');
		button('▸ Resolved (1)').click();
		flushSync();
		expect(target.textContent).toContain('old question');
	});

	it('flags a quote whose text is gone', () => {
		const g = new FakeGuest();
		g.threads = [thread('t1', { detached: true })];
		panel(g);
		expect(target.textContent).toContain('(text removed)');
	});

	it('offers Reply to everyone and Edit/Delete only on my own comments', () => {
		const g = new FakeGuest();
		g.threads = [thread('t1', {}, [comment('r1', { parent_id: 't1', gid: 'me', author: 'Ada (guest)' })])];
		panel(g);
		const labels = [...target.querySelectorAll('button')].map((b) => b.textContent?.trim());
		expect(labels.filter((l) => l === 'Reply')).toHaveLength(1);
		expect(labels.filter((l) => l === 'Edit')).toHaveLength(1);
		expect(labels.filter((l) => l === 'Delete')).toHaveLength(1);
		expect(target.textContent).toContain('You');
	});

	it('shows a reply at once as Sending…, sends it on Enter, and settles when the threads show it', () => {
		const g = new FakeGuest();
		g.threads = [thread('t1')];
		const p = panel(g);
		button('Reply').click();
		flushSync();
		const box = target.querySelector<HTMLTextAreaElement>('textarea[aria-label="Reply"]')!;
		type(box, 'first line');
		expect(key(box, 'Enter', true).defaultPrevented).toBe(false); // Shift+Enter: a new line
		expect(g.sendComment).not.toHaveBeenCalled();
		expect(key(box, 'Enter').defaultPrevented).toBe(true);
		expect(g.sendComment).toHaveBeenCalledTimes(1);
		const op = g.sendComment.mock.calls[0][0];
		expect(op).toMatchObject({ op: 'reply', parent_id: 't1', text: 'first line' });
		expect(String(op.id)).toMatch(/^[0-9a-f]{12}$/);
		expect(target.querySelector('[data-testid="comment-sending"]')).not.toBeNull();
		expect(target.textContent).toContain('first line');
		// the threads update that contains it
		g.threads = [thread('t1', {}, [comment(String(op.id), { parent_id: 't1', gid: 'me', text: 'first line' })])];
		p.rev++;
		flushSync();
		expect(target.querySelector('[data-testid="comment-sending"]')).toBeNull();
		expect(target.textContent?.match(/first line/g)).toHaveLength(1);
	});

	it('Escape cancels a reply', () => {
		const g = new FakeGuest();
		g.threads = [thread('t1')];
		panel(g);
		button('Reply').click();
		flushSync();
		key(target.querySelector('textarea')!, 'Escape');
		expect(target.querySelector('textarea')).toBeNull();
		expect(g.sendComment).not.toHaveBeenCalled();
	});

	it('edits my own comment, and asks before deleting it', () => {
		const g = new FakeGuest();
		g.threads = [thread('t1', { gid: 'me', author: 'Ada (guest)', text: 'mine' })];
		const p = panel(g);
		button('Edit').click();
		flushSync();
		const box = target.querySelector<HTMLTextAreaElement>('textarea')!;
		expect(box.value).toBe('mine');
		type(box, 'better');
		key(box, 'Enter');
		expect(g.sendComment).toHaveBeenLastCalledWith({ op: 'edit', id: 't1', text: 'better' });
		expect(target.textContent).toContain('better');
		expect(target.querySelector('[data-testid="comment-sending"]')).not.toBeNull();
		g.threads = [thread('t1', { gid: 'me', text: 'better' })];
		p.rev++;
		flushSync();
		button('Delete').click();
		flushSync();
		expect(target.textContent).toContain('Delete this comment?');
		expect(g.sendComment).toHaveBeenCalledTimes(1);
		button('Yes').click();
		flushSync();
		expect(g.sendComment).toHaveBeenLastCalledWith({ op: 'delete', id: 't1' });
		// a thread with nothing left in it is not shown
		expect(target.textContent).not.toContain('better');
	});

	it('shows a deleted reply as "Comment deleted" and keeps the thread', () => {
		const g = new FakeGuest();
		g.threads = [thread('t1', {}, [comment('r1', { parent_id: 't1', gid: 'me', deleted: true, text: '' })])];
		panel(g);
		expect(target.textContent).toContain('Comment deleted');
		expect(target.textContent).toContain('text t1');
	});

	it('composes a new thread on the selection: Send mints the id and sends create after the notes are synced', async () => {
		const g = new FakeGuest();
		const p = panel(g);
		p.draft = { text: 'the budget', first: 'aa', last: 'bb' };
		flushSync();
		expect(target.textContent).toContain('“the budget”');
		const box = target.querySelector<HTMLTextAreaElement>('textarea[aria-label="Your comment"]')!;
		type(box, 'why this number?');
		key(box, 'Enter');
		expect(p.ondone).toHaveBeenCalled();
		// shown at once, before the send goes out
		expect(target.querySelector('[data-testid="comment-sending"]')).not.toBeNull();
		expect(g.synced).toHaveBeenCalled();
		await vi.waitFor(() => expect(g.sendComment).toHaveBeenCalledTimes(1));
		const op = g.sendComment.mock.calls[0][0];
		expect(op).toMatchObject({ op: 'create', parent_id: '', text: 'why this number?', quote: 'the budget', notes_ids: ['aa', 'bb'] });
		expect(String(op.id)).toMatch(/^[0-9a-f]{12}$/);
	});

	it('Escape closes the compose box without sending', () => {
		const g = new FakeGuest();
		const p = panel(g);
		p.draft = { text: 'x', first: 'aa', last: 'bb' };
		flushSync();
		key(target.querySelector('textarea')!, 'Escape');
		expect(p.ondone).toHaveBeenCalled();
		expect(g.sendComment).not.toHaveBeenCalled();
	});

	it('sends what is still unseen again after a reconnect, three sends at most', () => {
		const g = new FakeGuest();
		g.threads = [thread('t1')];
		const p = panel(g);
		button('Reply').click();
		flushSync();
		const box = target.querySelector<HTMLTextAreaElement>('textarea')!;
		type(box, 'hello');
		key(box, 'Enter');
		expect(g.sendComment).toHaveBeenCalledTimes(1);
		for (let i = 2; i <= 5; i++) {
			g.catchUps++;
			p.rev++;
			flushSync();
			expect(g.sendComment).toHaveBeenCalledTimes(Math.min(i, 3));
		}
		// the same id each time
		const ids = new Set(g.sendComment.mock.calls.map((c) => c[0].id));
		expect(ids.size).toBe(1);
		// dropped after the third try: no longer shown
		expect(target.querySelector('[data-testid="comment-sending"]')).toBeNull();
	});

	it('does not send again what the catch-up already showed', () => {
		const g = new FakeGuest();
		g.threads = [thread('t1')];
		const p = panel(g);
		button('Reply').click();
		flushSync();
		const box = target.querySelector<HTMLTextAreaElement>('textarea')!;
		type(box, 'hello');
		key(box, 'Enter');
		const id = String(g.sendComment.mock.calls[0][0].id);
		g.threads = [thread('t1', {}, [comment(id, { parent_id: 't1', gid: 'me', text: 'hello' })])];
		g.catchUps++;
		p.rev++;
		flushSync();
		expect(g.sendComment).toHaveBeenCalledTimes(1);
	});

	it('read-only: threads stay, nothing can be added, replied to, edited or deleted', () => {
		const g = new FakeGuest();
		g.threads = [thread('t1', { gid: 'me', text: 'mine' })];
		panel(g, { canAct: false, draft: { text: 'x', first: 'a', last: 'b' } });
		expect(target.textContent).toContain('mine');
		expect(target.querySelector('textarea')).toBeNull();
		const labels = [...target.querySelectorAll('button')].map((b) => b.textContent?.trim());
		expect(labels).not.toContain('Reply');
		expect(labels).not.toContain('Edit');
		expect(labels).not.toContain('Delete');
	});

	it('renders nothing when read-only and there are no comments', () => {
		panel(new FakeGuest(), { canAct: false });
		expect(target.querySelector('h2')).toBeNull();
	});

	it('clicking a thread asks to show its mark; the quote button does too', () => {
		const g = new FakeGuest();
		g.threads = [thread('t1')];
		const p = panel(g);
		target.querySelector<HTMLElement>('[data-thread="t1"] .break-words')!.click();
		expect(p.ongoto).toHaveBeenCalledWith('t1');
		(p.ongoto as ReturnType<typeof vi.fn>).mockClear();
		target.querySelector<HTMLButtonElement>('[data-thread="t1"] button')!.click();
		expect(p.ongoto).toHaveBeenCalledTimes(1);
	});

	it('flashes the thread a click on its mark pointed at', async () => {
		const g = new FakeGuest();
		g.threads = [thread('t1'), thread('t2')];
		const p = panel(g);
		p.focus = { id: 't2', n: 1 } as never;
		flushSync();
		await vi.waitFor(() => expect(target.querySelector('[data-thread="t2"]')!.className).toContain('border-yellow-500'));
		expect(target.querySelector('[data-thread="t1"]')!.className).not.toContain('border-yellow-500');
	});
});
