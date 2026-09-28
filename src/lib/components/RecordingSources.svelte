<script lang="ts">
	import { restartCapture } from '$lib/app/controller.svelte.js';
	import { audioState } from '$lib/stores/audio.svelte.js';
	import LevelMeter from './LevelMeter.svelte';

	// While recording: one line naming the sources, with their levels, instead of the device list.
	const sources = $derived(audioState.devices.filter((d) => audioState.selectedDeviceIds.has(d.id)));
	const failing = $derived(Object.keys(audioState.problems).length > 0);
	const PROBLEM: Record<string, string> = { stopped: 'stopped', stalled: 'no audio' };
</script>

<div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-400" aria-label="Recording from">
	<span class="text-gray-600">Recording from</span>
	{#each sources as d (d.id)}
		<span class="flex items-center gap-1.5">
			<span class="text-gray-300">{d.description}</span>
			{#if audioState.problems[String(d.id)]}
				<span class="rounded bg-red-900/60 px-1.5 text-red-200">{PROBLEM[audioState.problems[String(d.id)]]}</span>
			{:else}
				<LevelMeter level={audioState.levels[String(d.id)]} compact />
			{/if}
		</span>
	{/each}
	{#if failing}
		<button
			onclick={restartCapture}
			disabled={!!audioState.pending}
			class="rounded bg-red-700 px-2 py-0.5 text-white hover:bg-red-600 disabled:opacity-50"
			title="Save what was recorded and carry on recording into this meeting"
		>
			Restart capture
		</button>
	{/if}
</div>
