<script lang="ts">
	// An image line's picture at full size (images in the notes): a fixed overlay over the page,
	// the same object URL the line shows, centred within 95% of the viewport. Escape, a click on the
	// backdrop or the close button closes it; NotesEditor gives focus back to the picture. Focus
	// stays on the close button while it is open (the only control).
	import { onMount } from 'svelte';

	let {
		src,
		label,
		onclose
	}: {
		src: string;
		/** The caption, else "Image": the dialog's name and the image's alt text. */
		label: string;
		onclose: () => void;
	} = $props();

	let closeBtn = $state<HTMLButtonElement>();

	onMount(() => {
		closeBtn?.focus({ preventScroll: true });
	});

	function onkeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			e.preventDefault();
			e.stopPropagation();
			onclose();
		} else if (e.key === 'Tab') {
			e.preventDefault();
			closeBtn?.focus({ preventScroll: true });
		}
	}
</script>

<!-- svelte-ignore a11y_click_events_have_key_events (Escape and the close button are the keyboard's way out) -->
<div
	class="ne-lightbox"
	role="dialog"
	aria-modal="true"
	aria-label={label}
	tabindex="-1"
	data-testid="notes-lightbox"
	{onkeydown}
	onclick={(e) => {
		if (e.target === e.currentTarget) onclose();
	}}
>
	<img {src} alt={label} />
	<button bind:this={closeBtn} type="button" class="ne-lightbox-close" aria-label="Close image" onclick={onclose}>×</button>
</div>
