<script lang="ts">
	import { audioState } from '$lib/stores/audio.svelte.js';

	$effect(() => {
		audioState.loadDevices();
	});
</script>

<div class="space-y-4">
	{#if audioState.loading}
		<p class="text-gray-400 text-sm">Loading devices...</p>
	{:else if audioState.error}
		<p class="text-red-400 text-sm">{audioState.error}</p>
	{:else}
		{#if audioState.inputDevices.length > 0}
			<div>
				<h3 class="text-sm font-medium text-gray-300 mb-2">Input Devices</h3>
				<div class="space-y-1">
					{#each audioState.inputDevices as device}
						<label
							class="flex items-center gap-2 px-3 py-2 rounded-lg hover:bg-gray-800 cursor-pointer transition-colors"
						>
							<input
								type="checkbox"
								checked={audioState.selectedDeviceIds.has(device.id)}
								onchange={() => audioState.toggleDevice(device.id)}
								disabled={audioState.isRecording}
								class="rounded border-gray-600 bg-gray-800 text-blue-500 focus:ring-blue-500"
							/>
							<span class="text-sm text-gray-200">{device.description}</span>
							{#if device.is_monitor}
								<span class="text-xs px-1.5 py-0.5 rounded bg-purple-900 text-purple-300"
									>Monitor</span
								>
							{/if}
						</label>
					{/each}
				</div>
			</div>
		{/if}

		{#if audioState.outputDevices.length > 0}
			<div>
				<h3 class="text-sm font-medium text-gray-300 mb-2">
					Output Devices <span class="text-gray-500">(system audio capture)</span>
				</h3>
				<div class="space-y-1">
					{#each audioState.outputDevices as device}
						<label
							class="flex items-center gap-2 px-3 py-2 rounded-lg hover:bg-gray-800 cursor-pointer transition-colors"
						>
							<input
								type="checkbox"
								checked={audioState.selectedDeviceIds.has(device.id)}
								onchange={() => audioState.toggleDevice(device.id)}
								disabled={audioState.isRecording}
								class="rounded border-gray-600 bg-gray-800 text-blue-500 focus:ring-blue-500"
							/>
							<span class="text-sm text-gray-200">{device.description}</span>
						</label>
					{/each}
				</div>
			</div>
		{/if}
	{/if}
</div>
