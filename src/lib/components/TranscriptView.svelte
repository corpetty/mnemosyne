<script lang="ts">
	import { transcriptState } from '$lib/stores/transcript.svelte.js';

	function formatTime(seconds: number): string {
		const m = Math.floor(seconds / 60);
		const s = Math.floor(seconds % 60);
		return `${m}:${String(s).padStart(2, '0')}`;
	}

	let container: HTMLDivElement;

	$effect(() => {
		// Auto-scroll to bottom when new segments arrive
		if (transcriptState.segments.length > 0 && container) {
			container.scrollTop = container.scrollHeight;
		}
	});
</script>

<div class="space-y-2">
	{#if transcriptState.status}
		<div class="flex items-center gap-2 text-sm text-gray-400">
			{#if transcriptState.isProcessing}
				<span class="w-2 h-2 rounded-full bg-yellow-500 animate-pulse"></span>
			{/if}
			<span>{transcriptState.status}</span>
		</div>
	{/if}

	{#if transcriptState.error}
		<p class="text-sm text-red-400">{transcriptState.error}</p>
	{/if}

	<div
		bind:this={container}
		class="max-h-[500px] overflow-y-auto space-y-3 pr-2"
	>
		{#each transcriptState.segments as segment}
			<div class="flex gap-3 text-sm">
				<div class="flex-shrink-0 w-20 text-right">
					<span class="text-gray-500 font-mono text-xs">
						{formatTime(segment.start)}
					</span>
				</div>
				<div class="flex-shrink-0 w-24">
					<span class="font-medium {transcriptState.getSpeakerColor(segment.speaker)}">
						{segment.speaker}
					</span>
				</div>
				<div class="flex-1 text-gray-200">
					{segment.text}
				</div>
			</div>
		{/each}

		{#if transcriptState.segments.length === 0 && !transcriptState.isProcessing}
			<p class="text-gray-500 text-sm text-center py-8">
				No transcript yet. Record audio and it will be transcribed automatically.
			</p>
		{/if}
	</div>
</div>
