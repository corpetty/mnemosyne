<script lang="ts">
	// The notes editor's touch bar (phase 11): on coarse pointers only, while the editor has focus,
	// a compact bar fixed to the bottom of the visual viewport, i.e. just above the on-screen
	// keyboard. Bold, Italic, Bulleted and Task lists, `/` (the menu at the caret) and Comment.
	// Read-only: Comment only. `shown` tells the editor the bar is up, `height` how much room it
	// takes (the caret is kept above it).
	//
	// Buttons keep the editor's focus and selection. A mouse: pointerdown and mousedown are
	// cancelled. A finger: cancelling pointerdown is not enough, the browser's tap gesture still
	// moves focus to the button (Chromium focuses on the tap even when the compatibility mousedown
	// is suppressed) and clears the selection; the editor's focusout then hid this bar before the
	// click arrived, so the button did nothing. So `touchstart` is cancelled (no tap gesture: no
	// focus change, no click) and the button acts on `touchend` when the finger is still on it.
	import { onMount } from 'svelte';
	import type { ToolbarActive } from './types.js';
	import { followKeyboard, watchCoarse } from './viewport.js';

	let {
		active,
		visible,
		readOnly,
		canComment,
		shown = $bindable(false),
		height = $bindable(0),
		ontoggle,
		onbullet,
		ontask,
		onslash,
		oncomment
	}: {
		active: ToolbarActive;
		/** The editor has focus (a read-only one: its selection is in the editor). */
		visible: boolean;
		readOnly: boolean;
		canComment: boolean;
		shown?: boolean;
		/** The bar's height while it shows (0 when hidden). */
		height?: number;
		ontoggle: (kind: 'bold' | 'italic') => void;
		onbullet: () => void;
		ontask: () => void;
		onslash: () => void;
		oncomment: () => void;
	} = $props();

	let coarse = $state(false);
	/** Bottom of the visual viewport as an inset from the layout viewport's bottom. */
	let inset = $state(0);

	const canShow = $derived(coarse && visible && (!readOnly || canComment));
	$effect(() => {
		shown = canShow;
	});

	onMount(() => {
		const a = watchCoarse((c) => (coarse = c));
		const b = followKeyboard((v) => (inset = v));
		return () => {
			a();
			b();
		};
	});

	/** A button's action from a click (mouse, keyboard) or a finger's tap that never moved focus. */
	function tap(node: HTMLElement, act: () => void) {
		let fn = act;
		let armed = false;
		/** When a touch last acted: a click following it (some browsers still send one) is the same tap. */
		let touched = 0;
		const keep = (e: Event) => e.preventDefault();
		const inside = (t: Touch | undefined) => {
			if (!t) return true;
			const r = node.getBoundingClientRect();
			if (!r.width && !r.height) return true;
			return t.clientX >= r.left && t.clientX <= r.right && t.clientY >= r.top && t.clientY <= r.bottom;
		};
		const start = (e: TouchEvent) => {
			if (e.touches.length !== 1) return;
			e.preventDefault();
			armed = true;
		};
		const move = (e: TouchEvent) => {
			if (armed && !inside(e.touches[0])) armed = false;
		};
		const end = (e: TouchEvent) => {
			if (!armed) return;
			armed = false;
			e.preventDefault();
			if (!inside(e.changedTouches[0])) return;
			touched = Date.now();
			fn();
		};
		const cancel = () => (armed = false);
		const click = (e: MouseEvent) => {
			if (Date.now() - touched < 800) return e.preventDefault();
			fn();
		};
		node.addEventListener('pointerdown', keep);
		node.addEventListener('mousedown', keep);
		node.addEventListener('touchstart', start, { passive: false });
		node.addEventListener('touchmove', move, { passive: true });
		node.addEventListener('touchend', end, { passive: false });
		node.addEventListener('touchcancel', cancel);
		node.addEventListener('click', click);
		return {
			update(next: () => void) {
				fn = next;
			},
			destroy() {
				node.removeEventListener('pointerdown', keep);
				node.removeEventListener('mousedown', keep);
				node.removeEventListener('touchstart', start);
				node.removeEventListener('touchmove', move);
				node.removeEventListener('touchend', end);
				node.removeEventListener('touchcancel', cancel);
				node.removeEventListener('click', click);
			}
		};
	}

	let barEl = $state<HTMLElement>();
	/** Is `node` in the bar? (focus moving to one of its buttons is not the editor losing it) */
	export function contains(node: Node | null): boolean {
		return !!node && !!barEl?.contains(node);
	}

	// Measured once it rendered (its buttons and the safe-area padding fix it; no ResizeObserver).
	$effect(() => {
		height = canShow && barEl ? barEl.offsetHeight : 0;
	});

	const btn = (on: boolean) =>
		`min-h-10 min-w-10 px-3 text-sm rounded border border-gray-700 ${on ? 'bg-gray-700 text-gray-100' : 'bg-gray-900 text-gray-300'}`;
</script>

{#if canShow}
	<div
		role="toolbar"
		aria-label="Format notes (touch)"
		data-testid="touch-bar"
		bind:this={barEl}
		style="bottom: {inset}px; padding-bottom: env(safe-area-inset-bottom, 0px);"
		class="fixed left-0 right-0 z-40 flex items-center gap-1.5 px-2 py-1.5 bg-gray-950 border-t border-gray-700"
	>
		{#if !readOnly}
			<button type="button" use:tap={() => ontoggle('bold')} aria-label="Bold" aria-pressed={active.bold} class="{btn(active.bold)} font-bold">B</button>
			<button type="button" use:tap={() => ontoggle('italic')} aria-label="Italic" aria-pressed={active.italic} class="{btn(active.italic)} italic">I</button>
			<button type="button" use:tap={onbullet} aria-label="Bulleted list" aria-pressed={active.kind === 'bullet'} class={btn(active.kind === 'bullet')}>•</button>
			<button type="button" use:tap={ontask} aria-label="Task list" aria-pressed={active.kind === 'task'} class={btn(active.kind === 'task')}>☐</button>
			<button type="button" use:tap={onslash} aria-label="Insert" aria-haspopup="listbox" class={btn(false)}>/</button>
		{/if}
		{#if canComment}
			<button type="button" use:tap={oncomment} aria-label="Comment on the selection" class="{btn(false)} ml-auto">Comment</button>
		{/if}
	</div>
{/if}
