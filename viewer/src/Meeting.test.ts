// The meeting as a reader sees it, rendered in jsdom from the vectors' plaintext manifest
// (decryption itself is load.test.ts, in Node, where WebCrypto exists).
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Manifest } from './format';
import Meeting from './Meeting.svelte';
import Viewer from './Viewer.svelte';
import vec from '../../backend/tests/vectors/publication_v1.json';
const manifest: Manifest = JSON.parse(vec.manifest.json);

let target: HTMLElement;
let component: ReturnType<typeof mount> | null = null;

function render(m: Manifest, loadClip = vi.fn(async () => new Uint8Array([1, 2, 3]))) {
	target = document.createElement('div');
	document.body.appendChild(target);
	component = mount(Meeting, { target, props: { manifest: m, loadClip } });
	flushSync();
	return loadClip;
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	target?.remove();
});

const text = () => target.textContent!.replace(/\s+/g, ' ');
/** Each list item's marker and text, joined by a space (they sit in separate flex cells). */
const items = (heading: string) => {
	const h = [...target.querySelectorAll('h2')].find((x) => x.textContent === heading)!;
	return [...h.parentElement!.querySelectorAll('li')].map((li) =>
		[...li.children].map((c) => c.textContent!.trim()).join(' ').replace(/\s+/g, ' ')
	);
};

describe('Meeting', () => {
	it('shows the title, meta line, summary and every section', () => {
		render(manifest);
		expect(target.querySelector('h1')!.textContent).toBe('Roadmap sync 🚀 – Café Zürich');
		expect(text()).toContain('22 Oct 2026 · 31 min · Zoë Müller, Corey, 李雷');
		// Markdown rendered by the app's own component.
		expect(target.querySelector('.md h2')!.textContent).toBe('Summary');
		expect(target.querySelector('.md table')).not.toBeNull();
		const headings = [...target.querySelectorAll('h2.uppercase')].map((h) => h.textContent);
		expect(headings).toEqual(['Decisions', 'Open questions', 'Action items', 'Chapters', 'Notes', 'Quotes']);
		expect(items('Decisions')).toEqual(['\u2714\ufe0e Ship on Friday 📦']); // text, not emoji, form
		expect(items('Open questions')).toEqual(['? Who owns the 日本語 docs?']);
		expect(items('Action items')).toEqual([
			'✓ Write the release notes · Zoë (done)',
			'○ Book the venue · due 1 Nov 2026'
		]);
		expect(items('Chapters')).toEqual(['0:00 Intro', '5:01 Plan ✅']);
		expect(text()).toContain('“Let\'s ship it — « vite ! »”');
		expect(text()).toContain('— Zoë Müller');
		expect([...target.querySelectorAll('.rounded-full')].map((c) => c.textContent)).toEqual(['launch', 'docs']);
		expect(target.querySelector('footer')!.textContent!.trim()).toMatch(
			/^Published with Mnemosyne · version 2 · expires \d+ Jan 2027$/
		);
		expect(target.textContent).not.toContain('undefined');
	});

	it('says "Preview" in the footer of a preview, with the version it would make', () => {
		const m = { ...manifest, version: 3, expires_at: null };
		target = document.createElement('div');
		document.body.appendChild(target);
		component = mount(Meeting, { target, props: { manifest: m, loadClip: vi.fn(), preview: true } });
		flushSync();
		expect(target.querySelector('footer')!.textContent!.trim()).toBe('Preview · version 3');
	});

	it('keeps the line breaks of a quote with several speakers', () => {
		const text = 'Ana: First.\nBo: Second.';
		render({ ...manifest, quotes: [{ ...manifest.quotes[1], text, speaker: null }] });
		const quote = target.querySelector('blockquote')!;
		expect(quote.textContent).toBe(`“${text}”`);
		expect(quote.classList.contains('whitespace-pre-line')).toBe(true);
	});

	it('strikes done action items', () => {
		render(manifest);
		const done = [...target.querySelectorAll('span.line-through')];
		expect(done.map((s) => s.textContent!.replace(/\s+/g, ' ').trim())).toEqual(['Write the release notes · Zoë (done)']);
	});

	it('leaves out sections that are empty', () => {
		render({
			...manifest,
			summary: '',
			decisions: [],
			questions: [],
			action_items: [],
			chapters: [],
			notes: null,
			quotes: [],
			topics: [],
			expires_at: null
		});
		expect(target.querySelectorAll('h2.uppercase').length).toBe(0);
		expect(target.querySelector('.md')).toBeNull();
		expect(target.querySelector('footer')!.textContent!.trim()).toBe('Published with Mnemosyne · version 2');
	});

	it('loads a clip only when play is pressed, then shows the audio element', async () => {
		const created: string[] = [];
		URL.createObjectURL = vi.fn(() => {
			created.push('blob:x');
			return 'blob:x';
		});
		URL.revokeObjectURL = vi.fn();
		window.HTMLMediaElement.prototype.play = vi.fn(async () => {});
		const loadClip = render(manifest);
		expect(target.querySelector('audio')).toBeNull();
		expect(loadClip).not.toHaveBeenCalled();
		const buttons = [...target.querySelectorAll('figure button')];
		expect(buttons.length).toBe(1); // the second quote has no clip
		expect(buttons[0].textContent).toBe('▶ 0:03');
		(buttons[0] as HTMLButtonElement).click();
		await vi.waitFor(() => expect(target.querySelector('audio')).not.toBeNull());
		expect(loadClip).toHaveBeenCalledWith(manifest.quotes[0].clip);
		expect(target.querySelector('audio')!.getAttribute('src')).toBe('blob:x');
		unmount(component!);
		component = null;
		expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:x');
	});
});

describe('Viewer', () => {
	it('shows the bare single-file template as an error', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		component = mount(Viewer, { target, props: { source: { kind: 'file', payload: '__PAYLOAD__' } } });
		await vi.waitFor(() => expect(target.textContent).toContain('This file could not be opened'));
	});

	it('shows the password prompt for a protected file', async () => {
		const payload = JSON.stringify({
			format: 1,
			id: vec.id,
			version: 2,
			manifest: vec.password.manifest_blob_b64url,
			clips: {},
			password: { salt: 'EBESExQVFhcYGRobHB0eHw', iterations: 600000 }
		});
		target = document.createElement('div');
		document.body.appendChild(target);
		component = mount(Viewer, { target, props: { source: { kind: 'file', payload } } });
		await vi.waitFor(() => expect(target.querySelector('input[type=password]')).not.toBeNull());
		expect(target.textContent).toContain('This meeting is protected with a password');
	});

	it('says the link is incomplete when the fragment is missing', async () => {
		window.history.replaceState(null, '', `/p/${vec.id}`);
		const fetchSpy = vi.fn();
		vi.stubGlobal('fetch', fetchSpy);
		target = document.createElement('div');
		document.body.appendChild(target);
		component = mount(Viewer, { target, props: { source: { kind: 'hosted' } } });
		await vi.waitFor(() =>
			expect(target.textContent).toContain('This link is incomplete: the part after # is missing')
		);
		expect(fetchSpy).not.toHaveBeenCalled();
		vi.unstubAllGlobals();
	});
});
