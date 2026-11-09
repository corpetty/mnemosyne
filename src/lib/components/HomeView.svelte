<script lang="ts">
	import { startRecording } from '$lib/app/controller.svelte.js';
	import DeviceSelector from './DeviceSelector.svelte';
	import { audioState } from '$lib/stores/audio.svelte.js';
	import { satelliteState } from '$lib/stores/satellite.svelte.js';
	import { connectionState } from '$lib/stores/connection.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { uiState } from '$lib/stores/ui.svelte.js';
</script>

<div class="flex-1 flex items-center justify-center text-gray-600">
	<div class="text-center space-y-4">
		<h2 class="text-2xl font-light text-gray-400">Mnemosyne</h2>
		<p class="text-sm text-gray-500">Real-time transcription, diarization, and summarization</p>
		{#if satelliteState.mainDown || (connectionState.usesMain && satelliteState.localUp && uiState.backendStatus === 'unreachable')}
			<!-- This computer uses a main computer and cannot reach it: it still records, into its outbox. -->
			<div class="space-y-3" role="status">
				<div class="flex items-center justify-center gap-2 text-amber-300">
					<span class="w-2 h-2 rounded-full bg-amber-400"></span>
					<span class="text-sm">Main computer not reachable. Recordings are kept here and go to it when it is.</span>
				</div>
				{#if audioState.isRecording}
					<p class="text-xs text-gray-500">Recording on this laptop. Stop it from the header.</p>
				{:else}
					<button
						onclick={() => startRecording(true)}
						disabled={!!audioState.pending}
						class="px-4 py-2 rounded-lg bg-red-600 hover:bg-red-700 text-white text-sm font-medium transition-colors disabled:opacity-60"
					>{audioState.pending === 'starting' ? 'Starting…' : 'Record'}</button>
					<div class="mx-auto max-w-md text-left">
						<DeviceSelector />
					</div>
				{/if}
			</div>
		{:else if uiState.backendStatus === 'checking'}
			<div class="space-y-2">
				<div class="flex items-center justify-center gap-2 text-yellow-500">
					<span class="w-2 h-2 rounded-full bg-yellow-500 animate-pulse"></span>
					<span class="text-sm">
						{#if uiState.shellStage === 'installing'}Installing backend (first run){:else if uiState.shellStage === 'starting'}Starting backend{:else}Connecting to backend{/if}...
					</span>
				</div>
				{#if uiState.shellStage === 'installing' && uiState.shellLog.length > 0}
					<pre class="mx-auto max-w-lg text-left text-[11px] leading-4 text-gray-600 bg-gray-900 border border-gray-800 rounded p-2 overflow-hidden whitespace-pre-wrap">{uiState.shellLog.join('\n')}</pre>
				{/if}
			</div>
		{:else if uiState.backendStatus === 'unreachable'}
			<div class="space-y-2">
				<div class="flex items-center justify-center gap-2 text-red-400">
					<span class="w-2 h-2 rounded-full bg-red-500"></span>
					<span class="text-sm">{uiState.shellStage === 'error' ? 'Backend failed to start' : `Backend unreachable at ${connectionState.url}`}</span>
				</div>
				{#if uiState.shellStage === 'error'}
					<pre class="mx-auto max-w-lg text-left text-[11px] leading-4 text-red-300/80 bg-gray-900 border border-gray-800 rounded p-2 whitespace-pre-wrap">{uiState.shellMessage}</pre>
				{:else}
					<p class="text-xs text-gray-600">Make sure the Python backend is running (still retrying)</p>
				{/if}
			</div>
		{:else}
			<button
				onclick={async () => { await sessionState.createSession(); }}
				disabled={sessionState.creating}
				class="px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium transition-colors disabled:opacity-60"
			>
				New meeting
			</button>
			<p class="text-xs text-gray-600">or open one from the sidebar</p>
		{/if}
		<div class="text-xs text-gray-700 space-y-1 mt-4">
			<p><kbd class="px-1 py-0.5 bg-gray-800 rounded text-gray-500">Ctrl+R</kbd> Start recording</p>
			<p><kbd class="px-1 py-0.5 bg-gray-800 rounded text-gray-500">Ctrl+S</kbd> Stop &amp; transcribe</p>
			<p><kbd class="px-1 py-0.5 bg-gray-800 rounded text-gray-500">Ctrl+E</kbd> Export to Obsidian</p>
			<p><kbd class="px-1 py-0.5 bg-gray-800 rounded text-gray-500">Ctrl+K</kbd> Find anything, or ask a question</p>
			<p><kbd class="px-1 py-0.5 bg-gray-800 rounded text-gray-500">Ctrl+B</kbd> Toggle sidebar</p>
		</div>
	</div>
</div>
