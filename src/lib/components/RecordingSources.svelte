<script lang="ts">
	import { audioState } from '$lib/stores/audio.svelte.js';
	import LevelMeter from './LevelMeter.svelte';

	// While recording: one line naming the sources, with their levels, instead of the device list.
	const sources = $derived(audioState.devices.filter((d) => audioState.selectedDeviceIds.has(d.id)));
</script>

<div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-400" aria-label="Recording from">
	<span class="text-gray-600">Recording from</span>
	{#each sources as d (d.id)}
		<span class="flex items-center gap-1.5">
			<span class="text-gray-300">{d.description}</span>
			<LevelMeter level={audioState.levels[String(d.id)]} compact />
		</span>
	{/each}
</div>
