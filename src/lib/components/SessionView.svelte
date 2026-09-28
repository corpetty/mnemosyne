<script lang="ts">
	import { markMoment, startRecording, stopAndTranscribe } from '$lib/app/controller.svelte.js';
	import { setLocalOnly } from '$lib/api/backend.js';
	import { audioState } from '$lib/stores/audio.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { uiState, type Tab } from '$lib/stores/ui.svelte.js';
	import AudioControls from './AudioControls.svelte';
	import AgendaCard from './AgendaCard.svelte';
	import CalendarCard from './CalendarCard.svelte';
	import CopilotPanel from './CopilotPanel.svelte';
	import MeetingActions from './MeetingActions.svelte';
	import MeetingTypePicker from './MeetingTypePicker.svelte';
	import MeetingBrief from './MeetingBrief.svelte';
	import DeviceSelector from './DeviceSelector.svelte';
	import LiveTranscript from './LiveTranscript.svelte';
	import RecordingSources from './RecordingSources.svelte';
	import NotesEditor from './NotesEditor.svelte';
	import ObsidianExport from './ObsidianExport.svelte';
	import SummaryView from './SummaryView.svelte';
	import TranscriptView from './TranscriptView.svelte';

	const nextPart = $derived(
		Math.max(0, ...(sessionState.activeSession?.recordings ?? []).map((r) => r.part)) + 2
	);

	// This meeting is being recorded right now.
	const recordingHere = $derived(
		audioState.isRecording && audioState.activeSessionId === sessionState.activeSession?.id
	);

	const tabs: { id: Tab; label: string }[] = [
		{ id: 'recording', label: 'Recording' },
		{ id: 'transcript', label: 'Transcript' },
		{ id: 'summary', label: 'Summary' },
		{ id: 'notes', label: 'Notes' },
		{ id: 'export', label: 'Export' }
	];

	async function toggleLocalOnly() {
		const s = sessionState.activeSession;
		if (!s) return;
		try {
			await setLocalOnly(s.id, !s.local_only);
			await sessionState.refreshActive();
			await sessionState.loadSessions();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not change the setting');
		}
	}
</script>

{#if sessionState.activeSession}
<!-- Session header + tabs -->
<div class="border-b border-gray-800 px-6 pt-4 pb-0 flex-shrink-0">
	<div class="flex items-center justify-between mb-3">
		<h2 class="text-xl font-semibold truncate">{sessionState.activeSession.name}</h2>
		<span class="flex items-center gap-3 text-xs text-gray-500 flex-shrink-0">
			<MeetingTypePicker />
			<MeetingActions />
			<button
				onclick={toggleLocalOnly}
				aria-pressed={sessionState.activeSession.local_only}
				title={sessionState.activeSession.local_only
					? 'Local-only: never sent to OpenAI or Anthropic. Click to allow.'
					: 'Mark local-only: never send this meeting to a cloud LLM'}
				class="px-2 py-0.5 rounded border transition-colors {sessionState.activeSession.local_only
					? 'border-amber-700 text-amber-300 bg-amber-950/40'
					: 'border-gray-800 text-gray-500 hover:text-gray-300'}"
			>
				{sessionState.activeSession.local_only ? '🔒 Local only' : '🔓 Cloud allowed'}
			</button>
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
	<div class={recordingHere && uiState.activeTab === 'recording' ? 'max-w-7xl' : 'max-w-4xl'}>
		{#if uiState.activeTab === 'recording'}
			{#if recordingHere}
				<!-- While recording: controls on top, then the live transcript and the copilot side by side. -->
				<div class="space-y-3">
					<div class="flex flex-wrap items-center gap-x-6 gap-y-2 rounded-lg border border-red-900/60 bg-red-950/20 px-3 py-2">
						<AudioControls onStartOverride={() => startRecording()} onStopOverride={stopAndTranscribe} />
						<button
							onclick={markMoment}
							title="Mark this moment as important (Ctrl+M); the summary gives it weight"
							class="rounded border border-amber-700/70 bg-amber-950/40 px-2.5 py-1 text-sm text-amber-200 hover:bg-amber-900/50"
						>
							🔖 Mark{audioState.marks ? ` · ${audioState.marks}` : ''}
						</button>
						<RecordingSources />
					</div>
					<!-- Side by side when wide; stacked (copilot, then the live transcript) when narrow. -->
					<div class="grid gap-4 lg:grid-cols-5">
						<div class="lg:col-span-2 lg:order-2"><CopilotPanel /></div>
						<div class="lg:col-span-3 lg:order-1"><LiveTranscript tall /></div>
					</div>
				</div>
			{:else}
				<div class="space-y-4">
					<CopilotPanel />
					<CalendarCard />
					<AgendaCard />
					{#if sessionState.activeSession.name !== 'Untitled Session' || sessionState.activeSession.attendees.length}
						<MeetingBrief
							title={sessionState.activeSession.name}
							attendees={sessionState.activeSession.attendees}
							exclude={sessionState.activeSession.id}
						/>
					{/if}
					<DeviceSelector />
					<AudioControls
						onStartOverride={() => startRecording()}
						onStopOverride={stopAndTranscribe}
					/>
					{#if sessionState.activeSession.audio_file}
						<p class="text-xs text-gray-500">
							Recording again adds to this meeting (part {nextPart}); what is already recorded, transcribed and summarized
							is kept.
						</p>
					{/if}
					<p class="text-xs text-gray-600">
						Shortcuts: <kbd class="px-1 py-0.5 bg-gray-800 rounded text-gray-400">Ctrl+R</kbd> Record
						&middot; <kbd class="px-1 py-0.5 bg-gray-800 rounded text-gray-400">Ctrl+S</kbd> Stop
					</p>
					<LiveTranscript />
				</div>
			{/if}
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
