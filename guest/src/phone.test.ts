// The phone layout in jsdom: the tab logic (layout.ts, Tabs.svelte), and the Comments panel's side of
// it: posting from the touch sheet through the pending path, and a thread in a bottom sheet.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Comments from './Comments.svelte';
import type { GuestComment, GuestDoc, GuestThread } from './doc';
import { isNarrow, NARROW_QUERY, tabForKey, tabsFor, watchNarrow } from './layout';
import { reactive } from './reactive.svelte';
import Tabs from './Tabs.svelte';

function stubMedia(matches: boolean) {
	let listener: (() => void) | null = null;
	const mq = {
		matches,
		addEventListener: (_: string, fn: () => void) => (listener = fn),
		removeEventListener: () => (listener = null)
	};
	const queries: string[] = [];
	window.matchMedia = ((q: string) => {
		queries.push(q);
		return mq;
	}) as unknown as typeof window.matchMedia;
	return {
		queries,
		set(v: boolean) {
			mq.matches = v;
			listener?.();
		},
		get listening() {
			return listener !== null
		}
	};
}
const realMatchMedia = window.matchMedia;

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

function render(C: typeof Comments | typeof Tabs, props: Record<string, unknown>) {
	target = document.createElement('div');
	document.body.appendChild(target);
	component = mount(C as typeof Comments, { target, props: props as never });
	flushSync();
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	target?.remove();
	document.body.innerHTML = '';
	window.matchMedia = realMatchMedia;
});

const q = <T extends Element = HTMLElement>(sel: string) => document.querySelector<T>(sel);
const type = (el: HTMLTextAreaElement, value: string) => {
	el.value = value;
	el.dispatchEvent(new Event('input', { bubbles: true }));
	flushSync();
};
const buttonIn = (root: ParentNode, label: string) =>
	[...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === label) as HTMLButtonElement;

describe('layout', () => {
	it('is narrow under the md breakpoint and follows changes', () => {
		const m = stubMedia(true);
		expect(isNarrow()).toBe(true);
		expect(m.queries.at(-1)).toBe(NARROW_QUERY);
		const seen: boolean[] = [];
		const stop = watchNarrow((n) => seen.push(n));
		m.set(false);
		expect(seen).toEqual([true, false]);
		stop();
		expect(m.listening).toBe(false);
	});

	it('is wide where matchMedia is missing', () => {
		// @ts-expect-error jsdom has no matchMedia
		window.matchMedia = undefined;
		expect(isNarrow()).toBe(false);
	});

	it('lists Notes, Agenda, Comments with the open count on the last', () => {
		expect(tabsFor(2).map((t) => [t.id, t.label, t.count])).toEqual([
			['notes', 'Notes', 0],
			['agenda', 'Agenda', 0],
			['comments', 'Comments', 2]
		]);
		expect(tabsFor(-1)[2].count).toBe(0);
	});

	it('moves between tabs with the arrows, wrapping, Home and End', () => {
		expect(tabForKey('notes', 'ArrowRight')).toBe('agenda');
		expect(tabForKey('comments', 'ArrowRight')).toBe('notes');
		expect(tabForKey('notes', 'ArrowLeft')).toBe('comments');
		expect(tabForKey('agenda', 'End')).toBe('comments');
		expect(tabForKey('agenda', 'Home')).toBe('notes');
		expect(tabForKey('agenda', 'a')).toBeNull();
	});
});

describe('Tabs', () => {
	const tabs = () => [...document.querySelectorAll<HTMLElement>('[role="tab"]')];

	it('has Notes selected by default and shows the count', () => {
		const props = reactive({ comments: 2 });
		render(Tabs, props);
		expect(q('[role="tablist"]')).not.toBeNull();
		expect(tabs().map((t) => t.textContent?.trim())).toEqual(['Notes', 'Agenda', 'Comments 2']);
		expect(tabs().map((t) => t.getAttribute('aria-selected'))).toEqual(['true', 'false', 'false']);
		expect(tabs().map((t) => t.getAttribute('aria-controls'))).toEqual([
			'guest-panel-notes',
			'guest-panel-agenda',
			'guest-panel-comments'
		]);
		props.comments = 0;
		flushSync();
		expect(tabs()[2].textContent?.trim()).toBe('Comments');
	});

	it('selects on click and on the arrow keys, with a roving tabindex', () => {
		render(Tabs, { comments: 0 });
		tabs()[2].click();
		flushSync();
		expect(tabs().map((t) => t.getAttribute('aria-selected'))).toEqual(['false', 'false', 'true']);
		expect(tabs().map((t) => t.tabIndex)).toEqual([-1, -1, 0]);
		tabs()[2].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }));
		flushSync();
		expect(tabs()[1].getAttribute('aria-selected')).toBe('true');
		expect(document.activeElement).toBe(tabs()[1]);
	});
});

describe('Comments on a phone', () => {
	function panel(g: FakeGuest, over: Record<string, unknown> = {}) {
		const props = reactive({
			guest: g as unknown as GuestDoc,
			rev: 0,
			canAct: true,
			draft: null,
			focus: null,
			ondone: vi.fn(),
			ongoto: vi.fn(),
			phone: true,
			visible: true,
			openCount: 0,
			...over
		});
		render(Comments, props);
		return props;
	}

	it('is a tabpanel that is hidden, not removed, when another tab shows', () => {
		const g = new FakeGuest();
		g.threads = [thread('t1')];
		const props = panel(g, { visible: false });
		const p = q('#guest-panel-comments')!;
		expect(p.getAttribute('role')).toBe('tabpanel');
		expect(p.getAttribute('aria-labelledby')).toBe('guest-tab-comments');
		expect(p.hidden).toBe(true);
		expect(p.textContent).toContain('text t1');
		props.visible = true;
		flushSync();
		expect(p.hidden).toBe(false);
	});

	it('has no tabpanel in the wide layout', () => {
		panel(new FakeGuest(), { phone: false });
		expect(q('[role="tabpanel"]')).toBeNull();
	});

	it('reports the open threads, a comment just posted included', async () => {
		const g = new FakeGuest();
		g.threads = [thread('t1'), thread('t2', { resolved: true })];
		const props = panel(g);
		expect(props.openCount).toBe(1);
		await (component as unknown as { post: Comments['post'] }).post({ text: 'x', first: 'a', last: 'b' }, 'hi');
		flushSync();
		expect(props.openCount).toBe(2);
	});

	it('shows "Show in notes" on a thread and hands its id on; a tap on the card does not', () => {
		const g = new FakeGuest();
		g.threads = [thread('t1'), thread('t2', { detached: true })];
		const props = panel(g);
		const shows = [...target.querySelectorAll('button')].filter((b) => b.textContent?.trim() === 'Show in notes');
		expect(shows).toHaveLength(1);
		shows[0].click();
		expect(props.ongoto).toHaveBeenCalledWith('t1');
		(target.querySelector('[data-thread="t1"] p') as HTMLElement).click();
		expect(props.ongoto).toHaveBeenCalledTimes(1);
	});

	it('says so when there are no comments', () => {
		panel(new FakeGuest(), { canAct: false });
		expect(q('[data-testid="guest-comments-none"]')?.textContent).toContain('No comments yet');
	});

	it('posts from the sheet through the pending path: Sending…, then sent', async () => {
		const g = new FakeGuest();
		panel(g);
		const post = (component as unknown as { post: Comments['post'] }).post;
		await post({ text: 'the plan', first: 'a1', last: 'a9' }, '  Looks off  ');
		flushSync();
		expect(g.synced).toHaveBeenCalled();
		expect(g.sendComment).toHaveBeenCalledTimes(1);
		const op = g.sendComment.mock.calls[0][0];
		expect(op).toMatchObject({ op: 'create', text: 'Looks off', quote: 'the plan', notes_ids: ['a1', 'a9'] });
		expect(q('[data-testid="comment-sending"]')).not.toBeNull();
		expect(target.textContent).toContain('Looks off');
	});

	it('is resent after a reconnect while not seen, like the compose box', async () => {
		const g = new FakeGuest();
		const props = panel(g);
		await (component as unknown as { post: Comments['post'] }).post({ text: 't', first: 'a', last: 'b' }, 'again');
		g.catchUps = 2;
		props.rev = 1;
		flushSync();
		expect(g.sendComment).toHaveBeenCalledTimes(2);
	});

	it('refuses to post when this page may not comment', async () => {
		const g = new FakeGuest();
		panel(g, { canAct: false });
		const post = (component as unknown as { post: Comments['post'] }).post;
		await expect(post({ text: 't', first: 'a', last: 'b' }, 'x')).rejects.toThrow();
		expect(g.sendComment).not.toHaveBeenCalled();
	});

	describe('thread sheet', () => {
		const open = (ids: string[]) =>
			(component as unknown as { openSheet: (ids: string[]) => void }).openSheet(ids);
		const sheet = () => q('[data-testid="thread-sheet"]');

		it('stays closed until opened, then shows the quote, the messages and a reply box', () => {
			const g = new FakeGuest();
			g.threads = [
				thread('t1', { text: 'first remark' }, [comment('r1', { parent_id: 't1', text: 'a reply' })]),
				thread('t2', { quote: 'other text' })
			];
			panel(g);
			expect(sheet()).toBeNull();
			open(['t1']);
			flushSync();
			const s = sheet()!;
			expect(s.getAttribute('role')).toBe('dialog');
			expect(s.querySelector('[data-testid="thread-sheet-quote"]')!.textContent).toContain('the plan');
			expect(s.textContent).toContain('first remark');
			expect(s.textContent).toContain('a reply');
			expect(s.textContent).not.toContain('text t2');
			expect(s.querySelector('textarea[aria-label="Reply"]')).not.toBeNull();
			expect(buttonIn(s, 'Close')).toBeTruthy();
		});

		it('shows every thread on the highlight, in the order given', () => {
			const g = new FakeGuest();
			g.threads = [thread('t1', { text: 'one' }), thread('t2', { text: 'two' })];
			panel(g);
			open(['t2', 't1']);
			flushSync();
			const order = [...sheet()!.querySelectorAll('[data-sheet-thread]')].map((e) => e.getAttribute('data-sheet-thread'));
			expect(order).toEqual(['t2', 't1']);
		});

		it('sends a reply through the pending path and keeps the sheet open', () => {
			const g = new FakeGuest();
			g.threads = [thread('t1')];
			panel(g);
			open(['t1']);
			flushSync();
			const s = sheet()!;
			expect(buttonIn(s, 'Send').disabled).toBe(true);
			type(s.querySelector('textarea')!, 'my reply');
			buttonIn(s, 'Send').click();
			flushSync();
			expect(g.sendComment).toHaveBeenCalledTimes(1);
			expect(g.sendComment.mock.calls[0][0]).toMatchObject({ op: 'reply', parent_id: 't1', text: 'my reply' });
			expect(sheet()).not.toBeNull();
			expect(sheet()!.textContent).toContain('my reply');
			expect(sheet()!.querySelector('[data-testid="comment-sending"]')).not.toBeNull();
			expect(sheet()!.querySelector('textarea')!.value).toBe('');
		});

		it('closes with Close and with Escape', () => {
			const g = new FakeGuest();
			g.threads = [thread('t1')];
			panel(g);
			open(['t1']);
			flushSync();
			buttonIn(sheet()!, 'Close').click();
			flushSync();
			expect(sheet()).toBeNull();
			open(['t1']);
			flushSync();
			sheet()!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
			flushSync();
			expect(sheet()).toBeNull();
		});

		it('lets a reader who cannot act read the thread but not reply', () => {
			const g = new FakeGuest();
			g.threads = [thread('t1')];
			panel(g, { canAct: false });
			open(['t1']);
			flushSync();
			expect(sheet()!.textContent).toContain('text t1');
			expect(sheet()!.querySelector('textarea')).toBeNull();
			expect(buttonIn(sheet()!, 'Close')).toBeTruthy();
		});

		it('does not open for no ids, and closes when the thread goes', () => {
			const g = new FakeGuest();
			g.threads = [thread('t1')];
			const props = panel(g);
			open([]);
			flushSync();
			expect(sheet()).toBeNull();
			open(['t1']);
			flushSync();
			expect(sheet()).not.toBeNull();
			g.threads = [thread('t1', { deleted: true })];
			props.rev = 1;
			flushSync();
			expect(sheet()).toBeNull();
		});
	});
});
