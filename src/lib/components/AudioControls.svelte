<script lang="ts">
	import { audioState } from '$lib/stores/audio.svelte.js';

	interface Props {
		onStartOverride?: () => Promise<void>;
		onStopOverride?: () => Promise<void>;
	}

	let { onStartOverride, onStopOverride }: Props = $props();

	function formatDuration(seconds: number): string {
		const h = Math.floor(seconds / 3600);
		const m = Math.floor((seconds % 3600) / 60);
		const s = seconds % 60;
		if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
		return `${m}:${String(s).padStart(2, '0')}`;
	}

	async function handleToggle() {
		if (audioState.isRecording) {
			if (onStopOverride) {
				await onStopOverride();
			} else {
				await audioState.stopRecording();
			}
		} else {
			if (onStartOverride) {
				await onStartOverride();
			} else {
				await audioState.startRecording();
			}
		}
	}
</script>

<div class="flex items-center gap-4">
	<button
		onclick={handleToggle}
		disabled={!audioState.isRecording && audioState.selectedDeviceIds.size === 0}
		class="flex items-center gap-2 px-4 py-2 rounded-lg font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed
			{audioState.isRecording
			? 'bg-red-600 hover:bg-red-700 text-white'
			: 'bg-blue-600 hover:bg-blue-700 text-white'}"
	>
		{#if audioState.isRecording}
			<span class="w-3 h-3 rounded-sm bg-white"></span>
			Stop
		{:else}
			<span class="w-3 h-3 rounded-full bg-red-400"></span>
			Record
		{/if}
	</button>

	{#if audioState.isRecording}
		<div class="flex items-center gap-2">
			<span class="w-2 h-2 rounded-full bg-red-500 animate-pulse"></span>
			<span class="text-sm font-mono text-gray-300">
				{formatDuration(audioState.recordingDuration)}
			</span>
		</div>
	{/if}

	{#if audioState.error}
		<p class="text-sm text-red-400">{audioState.error}</p>
	{/if}
</div>
