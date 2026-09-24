<script lang="ts">
	import { audioState } from '$lib/stores/audio.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { transcriptState } from '$lib/stores/transcript.svelte.js';
	import LevelMeter from './LevelMeter.svelte';
</script>

<footer class="border-t border-gray-800 px-4 py-1 flex items-center justify-between text-xs text-gray-600 flex-shrink-0">
	<div class="flex items-center gap-4">
		{#if audioState.isRecording}
			<div class="flex items-center gap-1.5 text-red-400">
				<span class="w-1.5 h-1.5 rounded-full bg-red-500 animate-pulse"></span>
				Recording
				{#each Object.entries(audioState.levels) as [id, lv] (id)}
					<LevelMeter level={lv} compact />
				{/each}
				<span class="font-mono">
					{Math.floor(audioState.recordingDuration / 60)}:{String(audioState.recordingDuration % 60).padStart(2, '0')}
				</span>
			</div>
		{/if}
		{#if transcriptState.isProcessing}
			<span class="text-yellow-500">Processing transcription...</span>
		{/if}
		{#if sessionState.activeSession}
			<span>{sessionState.activeSession.status}</span>
		{/if}
	</div>
	<div class="flex items-center gap-3">
		<span>{sessionState.sessions.length} session{sessionState.sessions.length !== 1 ? 's' : ''}</span>
	</div>
</footer>
