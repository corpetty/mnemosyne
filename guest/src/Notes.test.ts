// The guest page's notes view and name card in jsdom: the app's editor mounted on a stand-in for
// GuestDoc (the real one is doc.test.ts, in Node), its read-only switch and a remote edit.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ORIGIN, WasmHashWeb } from '$lib/notes/test-harness.js';
import { addComment, insertText } from '$lib/notes/commands.js';
import { openDoc, type Doc } from '$lib/notes/model.js';
import type { GuestDoc, GuestStatus } from './doc';
import Name from './Name.svelte';
import Notes from './Notes.svelte';
import { reactive } from './reactive.svelte';

class FakeGuest {
	doc: Doc;
	readOnly = false;
	status: GuestStatus = 'live';
	wrapRemote: ((apply: () => void) => void) | undefined;
	onRemoteChange: (() => void) | undefined;
	localChange = vi.fn();
	flush = vi.fn();
	setTyping = vi.fn();
	constructor() {
		this.doc = openDoc(new WasmHashWeb(), ORIGIN);
	}
	/** A change from the room, as GuestDoc applies one. */
	remote(fn: (doc: Doc) => void) {
		const w = this.wrapRemote;
		if (w) w(() => fn(this.doc));
		else fn(this.doc);
		this.onRemoteChange?.();
	}
}

let target: HTMLElement;
let component: ReturnType<typeof mount> | null = null;

function render(C: typeof Notes | typeof Name, props: Record<string, unknown>) {
	target = document.createElement('div');
	document.body.appendChild(target);
	component = mount(C as typeof Notes, { target, props: props as never });
	flushSync();
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	target?.remove();
});

const editor = () => target.querySelector<HTMLElement>('[aria-label="Meeting notes"]')!;

describe('Notes', () => {
	it('mounts the editor with its toolbar', () => {
		const g = new FakeGuest();
		render(Notes, { guest: g as unknown as GuestDoc, rev: 0 });
		expect(editor().getAttribute('aria-readonly')).toBe('false');
		expect(editor().getAttribute('role')).toBe('textbox');
		const names = [...target.querySelectorAll('[role="toolbar"] button')].map((b) => b.getAttribute('aria-label'));
		expect(names).toEqual([
			'Bold',
			'Italic',
			'Strikethrough',
			'Inline code',
			'Link',
			'Heading 1',
			'Heading 2',
			'Heading 3',
			'Bulleted list',
			'Numbered list',
			'Task list',
			'Insert table',
			'Undo',
			'Redo'
		]);
		expect(g.wrapRemote).toBeTypeOf('function');
	});

	it('renders a remote edit through wrapRemote', () => {
		const g = new FakeGuest();
		render(Notes, { guest: g as unknown as GuestDoc, rev: 0 });
		expect(editor().hasAttribute('data-empty')).toBe(true);
		g.remote((d) => insertText(d, 0, 'Hello\n'));
		expect(editor().textContent).toContain('Hello');
		expect(editor().hasAttribute('data-empty')).toBe(false);
	});

	it('is read-only, without a toolbar, once the notes ended', () => {
		const g = new FakeGuest();
		g.remote((d) => insertText(d, 0, 'Kept\n'));
		g.status = 'ended';
		g.readOnly = true;
		render(Notes, { guest: g as unknown as GuestDoc, rev: 1 });
		expect(editor().getAttribute('aria-readonly')).toBe('true');
		expect(target.querySelector('[role="toolbar"]')).toBeNull();
		expect(editor().textContent).toContain('Kept');
	});

	it('lets go of the document when unmounted', () => {
		const g = new FakeGuest();
		render(Notes, { guest: g as unknown as GuestDoc, rev: 0 });
		unmount(component!);
		component = null;
		expect(g.wrapRemote).toBeUndefined();
		expect(g.onRemoteChange).toBeUndefined();
		expect(g.setTyping).toHaveBeenCalledWith(false);
	});
});

describe('Notes comments', () => {
	function withComment() {
		const g = new FakeGuest();
		g.remote((d) => insertText(d, 0, 'see the plan here\n'));
		g.remote((d) => addComment(d, 4, 12, 'c1'));
		return g;
	}

	it('highlights only the marks of open threads, and follows them', () => {
		const g = withComment();
		const props = reactive({ guest: g as unknown as GuestDoc, rev: 0, openIds: new Set<string>() });
		render(Notes, props);
		const mark = () => editor().querySelector<HTMLElement>('mark.ne-comment')!;
		expect(mark().textContent).toBe('the plan');
		expect(mark().classList.contains('ne-comment-open')).toBe(false);
		props.openIds = new Set(['c1']);
		flushSync();
		expect(mark().classList.contains('ne-comment-open')).toBe(true);
		props.openIds = new Set();
		flushSync();
		expect(mark().classList.contains('ne-comment-open')).toBe(false);
	});

	it('a click on an open mark names its thread; a click on a plain one does nothing', () => {
		const g = withComment();
		const onmark = vi.fn();
		const props = reactive({ guest: g as unknown as GuestDoc, rev: 0, openIds: new Set<string>(), onmark });
		render(Notes, props);
		const mark = editor().querySelector<HTMLElement>('mark.ne-comment')!;
		mark.click();
		expect(onmark).not.toHaveBeenCalled();
		props.openIds = new Set(['c1']);
		flushSync();
		mark.click();
		expect(onmark).toHaveBeenCalledWith(['c1']);
	});

	it('goTo flashes the mark and reports whether it exists', () => {
		const g = withComment();
		render(Notes, { guest: g as unknown as GuestDoc, rev: 0, openIds: new Set(['c1']) });
		const notes = component as unknown as { goTo(id: string): boolean };
		expect(notes.goTo('c1')).toBe(true);
		expect(editor().querySelector('mark.ne-comment')!.classList.contains('ne-comment-flash')).toBe(true);
		expect(notes.goTo('nope')).toBe(false);
	});

	it('has no Comment button without a selection, or when commenting is off', () => {
		const g = withComment();
		render(Notes, { guest: g as unknown as GuestDoc, rev: 0, cancomment: true, oncompose: vi.fn() });
		expect(target.querySelector('[aria-label="Comment on the selection"]')).toBeNull();
	});

	it('is read-only while locked (a first visit before the name card is answered)', () => {
		const g = withComment();
		render(Notes, { guest: g as unknown as GuestDoc, rev: 0, locked: true });
		expect(editor().getAttribute('aria-readonly')).toBe('true');
		expect(target.querySelector('[role="toolbar"]')).toBeNull();
	});
});

describe('Name', () => {
	it('asks for a name, with the consent line, and joins with it trimmed', () => {
		const onjoin = vi.fn();
		render(Name, { owner: 'Corey', onjoin });
		expect(target.querySelector('h1')!.textContent).toContain("Join Corey's notes");
		expect(target.textContent).toContain("What you type here becomes part of Corey's notes of this meeting.");
		const input = target.querySelector('input')!;
		const join = [...target.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'Join')!;
		expect(join.disabled).toBe(true);
		input.value = '  Ada  ';
		input.dispatchEvent(new Event('input'));
		flushSync();
		expect(join.disabled).toBe(false);
		target.querySelector('form')!.dispatchEvent(new Event('submit', { cancelable: true }));
		expect(onjoin).toHaveBeenCalledWith('Ada');
	});

	it('says the meeting notes when the owner is unknown', () => {
		render(Name, { onjoin: () => {} });
		expect(target.querySelector('h1')!.textContent).toContain('Join the meeting notes');
		expect(target.querySelector('input')!.maxLength).toBe(80);
	});
});
