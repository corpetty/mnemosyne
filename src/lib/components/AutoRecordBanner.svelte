<script lang="ts">
	import { acceptAutoRecordOffer } from '$lib/app/controller.svelte.js';
	import { audioState } from '$lib/stores/audio.svelte.js';
	import { autoRecordState } from '$lib/stores/autorecord.svelte.js';
</script>

{#if autoRecordState.offer && !audioState.isRecording}
	<div class="flex items-center justify-between gap-3 border-b border-red-900 bg-red-950/40 px-4 py-2 text-sm flex-shrink-0" role="alert">
		<span class="text-red-100"><span class="font-medium">{autoRecordState.offer}</span> is using the microphone. Record this meeting?</span>
		<div class="flex items-center gap-2">
			<button onclick={acceptAutoRecordOffer} class="px-3 py-1 rounded bg-red-600 hover:bg-red-700 text-white text-xs font-medium">Record</button>
			<button onclick={() => (autoRecordState.offer = null)} class="text-xs text-red-300 hover:text-red-100">Not now</button>
		</div>
	</div>
{/if}
{#if autoRecordState.started && audioState.isRecording}
	<div class="border-b border-gray-800 bg-gray-900/60 px-4 py-1 text-xs text-gray-400 flex-shrink-0">
		Recording started automatically
		{#if autoRecordState.started.by === 'app'}(because {autoRecordState.started.app} is using the microphone); it stops a minute after {autoRecordState.started.app} does{:else}for the calendar meeting; it stops 5 minutes after it ends{/if}{#if autoRecordState.silenceMinutes > 0}, or after {autoRecordState.silenceMinutes} minutes of silence{/if}.
		<button onclick={() => (autoRecordState.started = null)} class="ml-2 underline hover:text-gray-200">Keep recording</button>
	</div>
{/if}
