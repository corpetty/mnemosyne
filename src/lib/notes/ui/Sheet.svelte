<script lang="ts">
	// A bottom sheet for touch screens: fixed to the bottom of the visual viewport, i.e. just above
	// the on-screen keyboard (like TouchBar.svelte), with the safe-area inset below its content.
	// Only the shell: what it shows comes in as `children` (CommentSheet.svelte writes a comment on
	// the selection; a thread can be shown in the same shell). Escape inside it calls `onclose`.
	import { onMount, type Snippet } from 'svelte';
	import { followKeyboard } from './viewport.js';

	let {
		label,
		testid,
		onclose,
		children
	}: {
		/** aria-label of the dialog. */
		label: string;
		testid?: string;
		onclose: () => void;
		children: Snippet;
	} = $props();

	/** Bottom of the visual viewport as an inset from the layout viewport's bottom. */
	let inset = $state(0);
	onMount(() => followKeyboard((v) => (inset = v)));

	function keydown(e: KeyboardEvent) {
		if (e.key !== 'Escape' || e.isComposing) return;
		e.preventDefault();
		e.stopPropagation();
		onclose();
	}
</script>

<div
	role="dialog"
	aria-label={label}
	data-testid={testid}
	tabindex="-1"
	onkeydown={keydown}
	style="bottom: {inset}px; padding-bottom: calc(0.5rem + env(safe-area-inset-bottom, 0px));"
	class="fixed left-0 right-0 z-50 max-h-[70vh] overflow-y-auto px-3 pt-2 bg-gray-950 border-t border-gray-700 rounded-t-lg shadow-lg"
>
	{@render children()}
</div>
