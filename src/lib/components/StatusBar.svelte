<script lang="ts">
	import { audioState } from '$lib/stores/audio.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { transcriptState } from '$lib/stores/transcript.svelte.js';
	import { uiState } from '$lib/stores/ui.svelte.js';
	import { openView } from '$lib/app/controller.svelte.js';
	import { ago, waitingNote } from '$lib/app/syncText.js';
	import { syncState } from '$lib/stores/sync.svelte.js';
	import LevelMeter from './LevelMeter.svelte';

	let tick = $state(Date.now());
	$effect(() => {
		const t = setInterval(() => (tick = Date.now()), 30_000);
		return () => clearInterval(t);
	});
	// The computer to show: one syncing, else one that is not fine, else the latest synced.
	const peer = $derived(
		syncState.peers.find((p) => p.state === 'syncing') ??
			syncState.peers.find((p) => p.state !== 'done' && p.state !== 'idle') ??
			[...syncState.peers].sort((a, b) => (b.last_sync_at ?? '').localeCompare(a.last_sync_at ?? ''))[0]
	);
	const syncText = $derived.by(() => {
		if (!peer) return '';
		if (peer.state === 'syncing') return `Syncing with ${peer.name}…`;
		if (peer.state === 'offline') return `${peer.name} offline`;
		if (peer.state === 'locked') return `${peer.name} locked`;
		if (peer.state === 'skew') return `${peer.name} needs an update`;
		if (peer.state === 'error') return `Sync with ${peer.name} failed`;
		return `Synced with ${peer.name} ${ago(peer.last_sync_at, tick)}${waitingNote(peer)}`;
	});
	function openSync() {
		uiState.settingsTab = 'general';
		openView('settings');
	}
</script>

<footer class="border-t border-gray-800 px-4 py-1 flex items-center justify-between text-xs text-gray-600 flex-shrink-0">
	<div class="flex items-center gap-4">
		{#if audioState.pending}
			<span class="text-gray-400">{audioState.pending === 'starting' ? 'Starting the recording…' : 'Stopping…'}</span>
		{/if}
		{#if audioState.isRecording}
			<div class="flex items-center gap-1.5 text-red-400">
				<span class="w-1.5 h-1.5 rounded-full bg-red-500"></span>
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
		{#if uiState.gpuInstall}
			<span
				class="truncate max-w-md {uiState.gpuInstall.state === 'error' ? 'text-red-400' : 'text-gray-500'}"
				title={uiState.gpuInstall.message}
			>
				{#if uiState.gpuInstall.state === 'installing'}Installing GPU support in the background (Parakeet works meanwhile): {uiState.gpuInstall.message}{:else if uiState.gpuInstall.state === 'error'}GPU support failed to install: {uiState.gpuInstall.message}{:else}{uiState.gpuInstall.message}{/if}
			</span>
		{/if}
	</div>
	<div class="flex items-center gap-3">
		{#if peer}
			<button onclick={openSync} title="Sync settings" class="hover:text-gray-300 {peer.state === 'error' || peer.state === 'skew' ? 'text-amber-400' : ''}">{syncText}</button>
		{/if}
		<span>{sessionState.sessions.length} meeting{sessionState.sessions.length !== 1 ? 's' : ''}</span>
	</div>
</footer>
