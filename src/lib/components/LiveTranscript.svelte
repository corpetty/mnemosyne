<script lang="ts">
	import { transcriptState } from '$lib/stores/transcript.svelte.js';
	import { audioState } from '$lib/stores/audio.svelte.js';

	function formatTime(seconds: number): string {
		const m = Math.floor(seconds / 60);
		const s = Math.floor(seconds % 60);
		return `${m}:${String(s).padStart(2, '0')}`;
	}

	let container = $state<HTMLDivElement>();
	$effect(() => {
		if ((transcriptState.liveSegments.length > 0 || Object.keys(transcriptState.livePartials).length) && container) {
			container.scrollTop = container.scrollHeight;
		}
	});

	const partials = $derived(Object.values(transcriptState.livePartials).filter((p) => p.text));
	const show = $derived(
		audioState.isRecording || transcriptState.liveSegments.length > 0 || partials.length > 0
	);
</script>

{#if show}
	<div class="rounded-lg border border-gray-800 bg-gray-900/60">
		<div class="flex items-center gap-2 px-3 py-1.5 border-b border-gray-800 text-xs text-gray-500">
			{#if audioState.isRecording}
				<span class="w-1.5 h-1.5 rounded-full bg-red-500 animate-pulse"></span>
				<span>Live transcript</span>
				{#if transcriptState.liveStatus}<span class="text-gray-600">· {transcriptState.liveStatus}</span>{/if}
			{:else}
				<span>Live transcript (provisional)</span>
			{/if}
		</div>
		<div bind:this={container} class="max-h-64 overflow-y-auto p-3 space-y-1.5 text-sm">
			{#each transcriptState.liveSegments as seg}
				<div class="flex gap-3">
					<span class="w-12 text-right text-xs font-mono text-gray-600 flex-shrink-0">{formatTime(seg.start)}</span>
					<span class="w-20 flex-shrink-0 font-medium {transcriptState.getSpeakerColor(seg.speaker)}">{seg.speaker}</span>
					<span class="text-gray-300">{seg.text}</span>
				</div>
			{/each}
			{#each partials as p}
				<div class="flex gap-3 italic text-gray-500">
					<span class="w-12 flex-shrink-0"></span>
					<span class="w-20 flex-shrink-0 {transcriptState.getSpeakerColor(p.speaker)} opacity-70">{p.speaker}</span>
					<span>{p.text}…</span>
				</div>
			{/each}
			{#if transcriptState.liveSegments.length === 0 && partials.length === 0}
				<p class="text-xs text-gray-600">Listening… text appears a few seconds behind speech.</p>
			{/if}
		</div>
	</div>
{/if}
