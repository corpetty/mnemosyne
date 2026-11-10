<script lang="ts">
	import { stopAndTranscribe } from '$lib/app/controller.svelte.js';
	import { audioState } from '$lib/stores/audio.svelte.js';
	import { satelliteState } from '$lib/stores/satellite.svelte.js';

	// A computer that uses a main computer (Settings → Devices and people → This computer): what to
	// say when the main computer cannot be reached, and the recording screen for a recording that
	// is kept on this computer until it can be (nothing to follow on the main computer yet).
	// Backend services/satellite.py; docs/plans/2026-11-09-connections-design.md, task 8.
	// Where the recording goes: the main computer this one uses, or the team it joined.
	const main = $derived(satelliteState.nameFor(audioState.satelliteDestination));
	const teamWaiting = $derived(
		satelliteState.teamDown &&
			(satelliteState.waitingFor('team').length > 0 ||
				(audioState.satellite && audioState.isRecording && audioState.satelliteDestination === 'team'))
	);
	const gap = $derived(!!satelliteState.recording?.sources.some((s) => s.gap) && !audioState.localOnly);
	const clock = $derived.by(() => {
		const sec = audioState.recordingDuration;
		const h = Math.floor(sec / 3600);
		const m = Math.floor((sec % 3600) / 60);
		const s = String(sec % 60).padStart(2, '0');
		return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
	});
</script>

{#if audioState.satellite && audioState.isRecording && audioState.localOnly}
	<div class="flex items-center justify-between gap-3 border-b border-red-900 bg-red-950/40 px-4 py-2 text-sm flex-shrink-0" role="status" aria-label="Recording on this computer">
		<span class="flex items-center gap-2 text-red-100">
			<span class="w-2 h-2 rounded-full bg-red-500"></span>
			<span>
				Recording on this computer. It goes to {main} when it can be reached.
				<span class="font-mono text-red-200">{clock}</span>
			</span>
		</span>
		<button
			onclick={stopAndTranscribe}
			disabled={!!audioState.pending}
			class="px-3 py-1 rounded bg-red-600 hover:bg-red-500 text-white text-xs font-medium disabled:opacity-60"
			title="Stop recording (Ctrl+S)"
		>{audioState.pending === 'stopping' ? 'Stopping…' : 'Stop'}</button>
	</div>
{:else if teamWaiting}
	<div class="border-b border-amber-900 bg-amber-950/30 px-4 py-1.5 text-xs text-amber-200 flex-shrink-0" role="status">
		{satelliteState.teamName} not reachable. Recordings for the team are kept here and go to it when it is.
		{#if audioState.satellite && audioState.isRecording && audioState.satelliteDestination === 'team'}
			This one keeps recording here.
		{/if}
	</div>
{:else if satelliteState.mainDown}
	<div class="border-b border-amber-900 bg-amber-950/30 px-4 py-1.5 text-xs text-amber-200 flex-shrink-0" role="status">
		Main computer not reachable. Recordings are kept here and go to it when it is.
		{#if audioState.satellite && audioState.isRecording}
			This one keeps recording here.
		{/if}
	</div>
{/if}
{#if gap}
	<div class="border-b border-gray-800 bg-gray-900/60 px-4 py-1 text-xs text-gray-400 flex-shrink-0" role="status">
		The connection to {main} dropped. Everything is recorded here and sent in full when you stop.
	</div>
{/if}
