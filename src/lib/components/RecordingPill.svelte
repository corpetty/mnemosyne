<script lang="ts">
	import { stopAndTranscribe } from '$lib/app/controller.svelte.js';
	import { audioState } from '$lib/stores/audio.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { uiState } from '$lib/stores/ui.svelte.js';

	// Always in the header while recording, whatever page is open: time, and Stop.
	function fmt(sec: number): string {
		const h = Math.floor(sec / 3600);
		const m = Math.floor((sec % 3600) / 60);
		const s = String(sec % 60).padStart(2, '0');
		return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
	}

	async function open() {
		if (!audioState.activeSessionId) return;
		if (sessionState.activeSession?.id !== audioState.activeSessionId) {
			await sessionState.selectSession(audioState.activeSessionId);
		}
		uiState.activeTab = 'recording';
	}
</script>

{#if audioState.isRecording || audioState.pending === 'starting'}
	<div class="shrink-0 flex items-center rounded-full border border-red-800 bg-red-950/60 text-xs">
		<button onclick={open} class="flex items-center gap-2 pl-3 pr-2 py-1 text-red-200 hover:text-white" title="Go to the recording">
			<span class="w-2 h-2 rounded-full bg-red-500"></span>
			<span class="font-mono">{audioState.isRecording ? fmt(audioState.recordingDuration) : '…'}</span>
		</button>
		<button
			onclick={stopAndTranscribe}
			disabled={!!audioState.pending}
			class="flex items-center gap-1.5 rounded-full bg-red-600 hover:bg-red-500 px-3 py-1 font-medium text-white disabled:opacity-60"
			title="Stop recording (Ctrl+S)"
		>
			<span class="w-2 h-2 rounded-sm bg-white"></span>
			{audioState.pending === 'stopping' ? 'Stopping…' : 'Stop'}
		</button>
	</div>
{/if}
