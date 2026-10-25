<script lang="ts">
	// The notes editor's touch bar (phase 11): on coarse pointers only, while the editor has focus,
	// a compact bar fixed to the bottom of the visual viewport, i.e. just above the on-screen
	// keyboard. Bold, Italic, Bulleted and Task lists, `/` (the menu at the caret) and Comment.
	// Read-only: Comment only. Buttons keep the editor's focus and selection (pointerdown and
	// mousedown are cancelled). `shown` tells the editor the bar is up (its own Comment button
	// then hides, so only one is visible).
	import { onMount } from 'svelte';
	import type { ToolbarActive } from './types.js';

	let {
		active,
		visible,
		readOnly,
		canComment,
		shown = $bindable(false),
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
		const mq = typeof matchMedia === 'function' ? matchMedia('(pointer: coarse)') : null;
		const onMq = () => (coarse = !!mq?.matches);
		onMq();
		mq?.addEventListener?.('change', onMq);
		const vv = typeof window !== 'undefined' ? window.visualViewport : null;
		const follow = () => {
			if (!vv) return (inset = 0);
			inset = Math.max(0, window.innerHeight - (vv.offsetTop + vv.height));
		};
		follow();
		vv?.addEventListener('resize', follow);
		vv?.addEventListener('scroll', follow);
		return () => {
			mq?.removeEventListener?.('change', onMq);
			vv?.removeEventListener('resize', follow);
			vv?.removeEventListener('scroll', follow);
		};
	});

	const keep = (e: Event) => e.preventDefault();
	const btn = (on: boolean) =>
		`min-h-10 min-w-10 px-3 text-sm rounded border border-gray-700 ${on ? 'bg-gray-700 text-gray-100' : 'bg-gray-900 text-gray-300'}`;
</script>

{#if canShow}
	<div
		role="toolbar"
		aria-label="Format notes (touch)"
		data-testid="touch-bar"
		style="bottom: {inset}px; padding-bottom: env(safe-area-inset-bottom, 0px);"
		class="fixed left-0 right-0 z-40 flex items-center gap-1.5 px-2 py-1.5 bg-gray-950 border-t border-gray-700"
	>
		{#if !readOnly}
			<button type="button" onpointerdown={keep} onmousedown={keep} onclick={() => ontoggle('bold')} aria-label="Bold" aria-pressed={active.bold} class="{btn(active.bold)} font-bold">B</button>
			<button type="button" onpointerdown={keep} onmousedown={keep} onclick={() => ontoggle('italic')} aria-label="Italic" aria-pressed={active.italic} class="{btn(active.italic)} italic">I</button>
			<button type="button" onpointerdown={keep} onmousedown={keep} onclick={onbullet} aria-label="Bulleted list" aria-pressed={active.kind === 'bullet'} class={btn(active.kind === 'bullet')}>•</button>
			<button type="button" onpointerdown={keep} onmousedown={keep} onclick={ontask} aria-label="Task list" aria-pressed={active.kind === 'task'} class={btn(active.kind === 'task')}>☐</button>
			<button type="button" onpointerdown={keep} onmousedown={keep} onclick={onslash} aria-label="Insert" aria-haspopup="listbox" class={btn(false)}>/</button>
		{/if}
		{#if canComment}
			<button type="button" onpointerdown={keep} onmousedown={keep} onclick={oncomment} aria-label="Comment on the selection" class="{btn(false)} ml-auto">Comment</button>
		{/if}
	</div>
{/if}
