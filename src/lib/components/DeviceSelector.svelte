<script lang="ts">
	import { audioState } from '$lib/stores/audio.svelte.js';
	import { getEchoCancel, setEchoCancel, type EchoCancelStatus } from '$lib/api/backend.js';
	import { toastState } from '$lib/stores/toast.svelte.js';

	let echo = $state<EchoCancelStatus | null>(null);
	let toggling = $state(false);

	async function loadEcho() {
		try {
			echo = await getEchoCancel();
		} catch {
			echo = null;
		}
	}

	async function toggleEcho() {
		if (!echo) return;
		toggling = true;
		try {
			echo = await setEchoCancel(!echo.active);
			await audioState.loadDevices();
			if (echo.active) {
				// Prefer the echo-cancelled mic over raw mics.
				const aec = audioState.devices.find((d) => d.is_echo_cancelled);
				if (aec) {
					for (const d of audioState.inputDevices) {
						if (audioState.selectedDeviceIds.has(d.id) && !d.is_echo_cancelled) audioState.toggleDevice(d.id);
					}
					if (!audioState.selectedDeviceIds.has(aec.id)) audioState.toggleDevice(aec.id);
				}
				toastState.success('Echo cancellation on: use "Mnemosyne: mic (echo cancelled)"');
			} else {
				toastState.info('Echo cancellation off');
			}
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not change echo cancellation');
		} finally {
			toggling = false;
		}
	}

	$effect(() => {
		audioState.loadDevices();
		loadEcho();
	});
</script>

{#if echo && echo.supported}
	<div class="flex items-center justify-between gap-3 rounded-lg border border-gray-800 bg-gray-900/60 px-3 py-2">
		<div class="text-xs text-gray-400">
			<span class="font-medium text-gray-300">Echo cancellation</span>
			<span class="text-gray-600"> · removes what your speakers play from the mic, so you can record without headphones</span>
		</div>
		<button
			onclick={toggleEcho}
			disabled={toggling || audioState.isRecording}
			class="px-3 py-1 text-xs rounded border transition-colors disabled:opacity-50 {echo.active ? 'bg-green-900/40 border-green-700 text-green-300' : 'bg-gray-800 border-gray-700 text-gray-300 hover:bg-gray-700'}"
		>
			{echo.active ? 'On' : 'Enable'}
		</button>
	</div>
{:else if echo && !echo.supported}
	<p class="text-[11px] text-gray-600">Echo cancellation unavailable: {echo.reason}</p>
{/if}

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
							{#if device.is_echo_cancelled}
								<span class="text-xs px-1.5 py-0.5 rounded bg-green-900 text-green-300">Echo-cancelled</span>
							{/if}
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
