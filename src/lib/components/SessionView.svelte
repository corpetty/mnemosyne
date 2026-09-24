<script lang="ts">
	import { startRecording, stopAndTranscribe } from '$lib/app/controller.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { uiState, type Tab } from '$lib/stores/ui.svelte.js';
	import AudioControls from './AudioControls.svelte';
	import CalendarCard from './CalendarCard.svelte';
	import DeviceSelector from './DeviceSelector.svelte';
	import LiveTranscript from './LiveTranscript.svelte';
	import NotesEditor from './NotesEditor.svelte';
	import ObsidianExport from './ObsidianExport.svelte';
	import SummaryView from './SummaryView.svelte';
	import TranscriptView from './TranscriptView.svelte';

	const tabs: { id: Tab; label: string }[] = [
		{ id: 'recording', label: 'Recording' },
		{ id: 'transcript', label: 'Transcript' },
		{ id: 'summary', label: 'Summary' },
		{ id: 'notes', label: 'Notes' },
		{ id: 'export', label: 'Export' }
	];
</script>

{#if sessionState.activeSession}
<!-- Session header + tabs -->
<div class="border-b border-gray-800 px-6 pt-4 pb-0 flex-shrink-0">
	<div class="flex items-center justify-between mb-3">
		<h2 class="text-xl font-semibold truncate">{sessionState.activeSession.name}</h2>
		<span class="text-xs text-gray-500 flex-shrink-0">
			{new Date(sessionState.activeSession.created_at).toLocaleString()}
		</span>
	</div>
	{#if sessionState.activeSession.attendees.length}
		<p class="-mt-2 mb-2 text-xs text-gray-500 truncate">Invited: {sessionState.activeSession.attendees.join(', ')}</p>
	{/if}
	<nav class="flex gap-1">
		{#each tabs as tab}
			<button
				onclick={() => (uiState.activeTab = tab.id)}
				class="px-3 py-1.5 text-sm rounded-t-lg transition-colors border-b-2
					{uiState.activeTab === tab.id
					? 'border-blue-500 text-blue-400 bg-gray-900'
					: 'border-transparent text-gray-500 hover:text-gray-300 hover:bg-gray-900/50'}"
			>
				{tab.label}
				{#if tab.id === 'summary' && sessionState.activeSession.summary_stale}
					<span class="ml-1 inline-block w-1.5 h-1.5 rounded-full bg-yellow-500 align-middle" title="Summary is out of date"></span>
				{/if}
				{#if tab.id === 'transcript' && sessionState.activeSession.transcript.length > 0}
					<span class="ml-1 text-xs text-gray-600">({sessionState.activeSession.transcript.length})</span>
				{/if}
			</button>
		{/each}
	</nav>
</div>

<!-- Tab content -->
<div class="flex-1 overflow-y-auto p-6">
	<div class="max-w-4xl">
		{#if uiState.activeTab === 'recording'}
			<div class="space-y-4">
				<CalendarCard />
				<DeviceSelector />
				<AudioControls
					onStartOverride={() => startRecording()}
					onStopOverride={stopAndTranscribe}
				/>
				<p class="text-xs text-gray-600">
					Shortcuts: <kbd class="px-1 py-0.5 bg-gray-800 rounded text-gray-400">Ctrl+R</kbd> Record
					&middot; <kbd class="px-1 py-0.5 bg-gray-800 rounded text-gray-400">Ctrl+S</kbd> Stop
				</p>
				<LiveTranscript />
			</div>
		{:else if uiState.activeTab === 'transcript'}
			<TranscriptView />
		{:else if uiState.activeTab === 'summary'}
			<SummaryView />
		{:else if uiState.activeTab === 'notes'}
			<NotesEditor />
		{:else if uiState.activeTab === 'export'}
			<ObsidianExport />
		{/if}
	</div>
</div>
{/if}
