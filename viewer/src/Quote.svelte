<script lang="ts">
	import { onDestroy } from 'svelte';
	import { formatClock } from './display';
	import type { Quote } from './format';
	import type { ClipLoader } from './load';

	let { quote, loadClip }: { quote: Quote; loadClip: ClipLoader } = $props();

	// The clip is fetched and decrypted only when the reader presses play; the audio element
	// appears then, playing a blob URL that is revoked when the page or the quote goes away.
	let url = $state<string | null>(null);
	let busy = $state(false);
	let error = $state<string | null>(null);
	let audio = $state<HTMLAudioElement | null>(null);

	async function play() {
		if (!quote.clip || busy) return;
		if (audio) {
			audio.play().catch(() => {});
			return;
		}
		busy = true;
		error = null;
		try {
			const bytes = await loadClip(quote.clip);
			url = URL.createObjectURL(new Blob([bytes as BlobPart], { type: quote.clip.mime }));
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not play this clip';
		} finally {
			busy = false;
		}
	}

	$effect(() => {
		// Start once the element exists; if the browser refuses autoplay, its controls remain.
		audio?.play().catch(() => {});
	});

	function revoke() {
		if (url) URL.revokeObjectURL(url);
		url = null;
	}
	onDestroy(revoke);
</script>

<svelte:window onpagehide={revoke} />

<figure class="border-l-2 border-gray-700 pl-3">
	<blockquote class="whitespace-pre-line text-sm text-gray-200">“{quote.text}”</blockquote>
	<figcaption class="mt-1 text-xs text-gray-500">
		<div class="flex flex-wrap items-center gap-x-2 gap-y-1">
			{#if quote.speaker}<span>— {quote.speaker}</span>{/if}
			{#if quote.clip && !url}
				<button
					type="button"
					onclick={play}
					disabled={busy}
					aria-label="Play the clip ({formatClock(quote.clip.seconds)})"
					class="rounded border border-gray-700 px-1.5 py-0.5 text-gray-400 hover:border-gray-500 hover:text-gray-200 disabled:opacity-50"
					>{busy ? '…' : '▶'} {formatClock(quote.clip.seconds)}</button
				>
			{/if}
			{#if error}<span class="text-red-400">{error}</span>{/if}
		</div>
		{#if url}
			<audio bind:this={audio} src={url} controls preload="auto" class="mt-2 h-8 w-full max-w-sm"></audio>
		{/if}
	</figcaption>
</figure>
