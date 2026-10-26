// The guest page's phone layout: below Tailwind's `md` (768 px) the notes, agenda and comments are
// tabs instead of columns. The pure parts live here for the tests; App.svelte holds the state.

/** Tailwind's `md` is min-width 768 px, so narrow is everything under it. */
export const NARROW_QUERY = '(max-width: 767.98px)';

/** Is the window narrower than `md`? False where matchMedia is missing (jsdom without a stub). */
export function isNarrow(): boolean {
	return typeof matchMedia === 'function' && matchMedia(NARROW_QUERY).matches;
}

/** Calls `fn` with `isNarrow()` now and whenever it changes; returns the unsubscribe. */
export function watchNarrow(fn: (narrow: boolean) => void): () => void {
	const mq = typeof matchMedia === 'function' ? matchMedia(NARROW_QUERY) : null;
	const on = () => fn(!!mq?.matches);
	on();
	mq?.addEventListener?.('change', on);
	return () => mq?.removeEventListener?.('change', on);
}

export type TabId = 'notes' | 'agenda' | 'comments';

export interface TabInfo {
	id: TabId;
	label: string;
	/** Shown after the label when above 0 (open threads on the Comments tab). */
	count: number;
}

export const TAB_IDS: readonly TabId[] = ['notes', 'agenda', 'comments'];

/** The three tabs, in order; `openThreads` is the Comments tab's count. */
export function tabsFor(openThreads: number): TabInfo[] {
	return [
		{ id: 'notes', label: 'Notes', count: 0 },
		{ id: 'agenda', label: 'Agenda', count: 0 },
		{ id: 'comments', label: 'Comments', count: Math.max(0, openThreads) }
	];
}

/** The tab a key moves to from `current` (arrows wrap, Home/End), or null for any other key. */
export function tabForKey(current: TabId, key: string): TabId | null {
	const i = TAB_IDS.indexOf(current);
	switch (key) {
		case 'ArrowRight':
			return TAB_IDS[(i + 1) % TAB_IDS.length];
		case 'ArrowLeft':
			return TAB_IDS[(i + TAB_IDS.length - 1) % TAB_IDS.length];
		case 'Home':
			return TAB_IDS[0];
		case 'End':
			return TAB_IDS[TAB_IDS.length - 1];
		default:
			return null;
	}
}

/** Element ids a tab and its panel share (aria-controls / aria-labelledby). */
export const tabId = (id: TabId) => `guest-tab-${id}`;
export const panelId = (id: TabId) => `guest-panel-${id}`;
