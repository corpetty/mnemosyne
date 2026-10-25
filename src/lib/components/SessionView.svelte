<script lang="ts">
	import { tick } from 'svelte';
	import { markMoment, startRecording, stopAndTranscribe } from '$lib/app/controller.svelte.js';
	import { setLocalOnly } from '$lib/api/backend.js';
	import { audioReason, canChange } from '$lib/app/access.js';
	import { syncState } from '$lib/stores/sync.svelte.js';
	import { connectionState } from '$lib/stores/connection.svelte.js';
	import { audioState } from '$lib/stores/audio.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { uiState, type Tab } from '$lib/stores/ui.svelte.js';
	import AudioControls from './AudioControls.svelte';
	import AgendaCard from './AgendaCard.svelte';
	import ResourcesCard from './ResourcesCard.svelte';
	import HistoryCard from './HistoryCard.svelte';
	import RecordsCard from './RecordsCard.svelte';
	import SupervisionCard from './SupervisionCard.svelte';
	import CalendarCard from './CalendarCard.svelte';
	import CopilotPanel from './CopilotPanel.svelte';
	import MeetingActions from './MeetingActions.svelte';
	import ShareMenu from './ShareMenu.svelte';
	import GuestNotesMenu from './GuestNotesMenu.svelte';
	import PeopleShareMenu from './PeopleShareMenu.svelte';
	import { contactsState } from '$lib/stores/contacts.svelte.js';
	import PublishDialog from './PublishDialog.svelte';
	import { publishingState } from '$lib/stores/publishing.svelte.js';
	import { teamState } from '$lib/stores/team.svelte.js';
	import MeetingTypePicker from './MeetingTypePicker.svelte';
	import MeetingBrief from './MeetingBrief.svelte';
	import DeviceSelector from './DeviceSelector.svelte';
	import BrowserSources from './BrowserSources.svelte';
	import LiveTranscript from './LiveTranscript.svelte';
	import RecordingSources from './RecordingSources.svelte';
	import NotesEditor from './NotesEditor.svelte';
	import ObsidianExport from './ObsidianExport.svelte';
	import SummaryView from './SummaryView.svelte';
	import TranscriptView from './TranscriptView.svelte';

	const nextPart = $derived(
		Math.max(0, ...(sessionState.activeSession?.recordings ?? []).map((r) => r.part)) + 2
	);

	// Someone else's meeting (a reviewer reading a member's): no controls the server refuses.
	const mine = $derived(canChange(sessionState.activeSession));
	// A meeting recorded on another computer: its audio, and recording into it, stay there.
	const foreign = $derived(
		!!(sessionState.activeSession?.origin || sessionState.activeSession?.shared_from)
	);
	// A meeting another person shared with me, and how the share and my work on it stand.
	const sharedFrom = $derived(sessionState.activeSession?.shared_from ?? '');
	const shareStatus = $derived(
		sessionState.activeSession ? (contactsState.shareStatus[sessionState.activeSession.id] ?? null) : null
	);
	$effect(() => {
		const id = sessionState.activeSession?.id;
		if (id && sharedFrom) void contactsState.loadStatus(id);
	});
	function plural(n: number, one: string, many = one + 's'): string {
		return `${n} ${n === 1 ? one : many}`;
	}
	function list(parts: string[]): string {
		return parts.length > 1 ? `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}` : (parts[0] ?? '');
	}
	const canDoLine = $derived.by(() => {
		if (!shareStatus) return '';
		const can = ['comment'];
		if (shareStatus.parts.action_items) can.push('tick action items');
		if (shareStatus.parts.notes) can.push('type in the notes');
		return `${contactsState.name(sharedFrom)} shared this with you: you can ${list(can)}.`;
	});
	const waitingLine = $derived.by(() => {
		const w = shareStatus?.waiting;
		if (!w) return '';
		const bits: string[] = [];
		if (w.comments) bits.push(plural(w.comments, 'comment'));
		if (w.ticks) bits.push(plural(w.ticks, 'tick'));
		if (w.notes) bits.push('notes edits');
		return bits.length ? `Waiting for ${contactsState.name(sharedFrom)}'s computer: ${bits.join(', ')}` : '';
	});
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

	// Whether publishing is on (a team server's setting), and the open meeting's publication
	// for the header chip; reloaded when the meeting changes, since `behind` follows it.
	$effect(() => {
		if (!publishingState.settingsLoaded) void publishingState.loadSettings();
	});
	$effect(() => {
		const s = sessionState.activeSession;
		if (!s) return;
		// A re-summarize or an edit can put the publication behind: the meeting's own updated_at,
		// and its sidebar entry's (reloaded on every session event, from any client).
		void s.updated_at;
		void sessionState.sessions.find((x) => x.id === s.id)?.updated_at;
		void publishingState.load(s.id);
	});
	const publication = $derived(
		sessionState.activeSession ? (publishingState.publications[sessionState.activeSession.id] ?? null) : null
	);
	const canPublish = $derived(mine && publishingState.allowed);

	// Renaming the open meeting in its header: Enter or blur saves, Escape cancels.
	let editing = $state(false);
	let draft = $state('');
	let savingName = $state(false);
	let nameInput = $state<HTMLInputElement>();

	function startEdit() {
		if (!sessionState.activeSession) return;
		draft = sessionState.activeSession.name;
		editing = true;
		void tick().then(() => {
			nameInput?.focus();
			nameInput?.select();
		});
	}
	function cancelEdit() {
		editing = false;
		savingName = false;
	}
	async function saveName() {
		const s = sessionState.activeSession;
		// Enter is followed by a blur when the input goes away: save once.
		if (!editing || savingName || !s) return;
		const name = draft.trim();
		if (!name || name === s.name) {
			cancelEdit();
			return;
		}
		savingName = true;
		await sessionState.renameSession(s.id, name);
		cancelEdit();
	}
	// Leaving the meeting while editing drops the edit.
	let editingFor = '';
	$effect(() => {
		const id = sessionState.activeSession?.id ?? '';
		if (id !== editingFor) {
			editingFor = id;
			cancelEdit();
		}
	});

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
		<h2 class="group flex min-w-0 flex-1 items-center gap-2 mr-4 text-xl font-semibold">
			{#if mine && editing}
				<input
					bind:this={nameInput}
					bind:value={draft}
					disabled={savingName}
					aria-label="Meeting name"
					onkeydown={(e) => {
						if (e.key === 'Enter') {
							e.preventDefault();
							void saveName();
						} else if (e.key === 'Escape') {
							e.preventDefault();
							cancelEdit();
						}
					}}
					onblur={() => void saveName()}
					class="flex-1 min-w-0 text-xl font-semibold bg-gray-900 border border-gray-700 rounded px-2 py-0.5"
				/>
			{:else if mine}
				<button
					type="button"
					onclick={startEdit}
					title="Rename this meeting"
					class="min-w-0 truncate text-left"
				>{sessionState.activeSession.name}</button>
				<button
					type="button"
					onclick={startEdit}
					tabindex="-1"
					aria-hidden="true"
					class="text-gray-500 text-sm opacity-0 group-hover:opacity-100 group-focus-within:opacity-100"
				>✎</button>
			{:else}
				<span class="truncate">{sessionState.activeSession.name}</span>
			{/if}
		</h2>
		<span class="flex items-center gap-3 text-xs text-gray-500 flex-shrink-0">
			{#if publication}
				{#if canPublish}
					<button
						onclick={() => sessionState.activeSession && publishingState.open(sessionState.activeSession)}
						title="Published: change, update or unpublish"
						class="rounded border border-emerald-800 bg-emerald-950/40 px-2 py-0.5 text-xs text-emerald-300 hover:bg-emerald-900/40"
					>Published · v{publication.version}{#if publication.behind}<span class="text-amber-300">{' · behind'}</span>{/if}</button>
				{:else}
					<span
						title="A copy of this meeting is published"
						class="rounded border border-emerald-800 bg-emerald-950/40 px-2 py-0.5 text-xs text-emerald-300"
					>Published · v{publication.version}{#if publication.behind}<span class="text-amber-300">{' · behind'}</span>{/if}</span>
				{/if}
			{/if}
			<ShareMenu />
			<PeopleShareMenu />
			<GuestNotesMenu />
			{#if mine}
				<MeetingTypePicker />
				<MeetingActions />
			{/if}
			<!-- With cloud models off on the server there is nothing to choose. -->
			{#if mine && connectionState.me?.cloud_models !== false}
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
			{/if}
			{#if sharedFrom}
				<span
					title="Shared with you from {contactsState.name(sharedFrom)}'s computer"
					class="rounded border border-violet-800 bg-violet-950/40 px-2 py-0.5 text-xs text-violet-300"
				>Shared by {contactsState.name(sharedFrom)}</span>
			{/if}
			{new Date(sessionState.activeSession.created_at).toLocaleString()}
		</span>
	</div>
	{#if sessionState.activeSession.origin}
		<p class="-mt-2 mb-2 text-xs text-gray-500">Recorded on {syncState.name(sessionState.activeSession.origin)}</p>
	{/if}
	{#if sharedFrom && shareStatus}
		<p class="-mt-2 mb-2 text-xs text-gray-500">{canDoLine}</p>
		{#if waitingLine}<p class="-mt-1 mb-2 text-xs text-gray-500">{waitingLine}</p>{/if}
		{#if shareStatus.rejected.length}
			<ul class="-mt-1 mb-2 space-y-0.5 text-xs text-amber-300/80">
				{#each shareStatus.rejected.slice(0, 3) as rj}
					<li>Not accepted by {contactsState.name(sharedFrom)}: {rj.excerpt ? `“${rj.excerpt}” — ` : ''}{rj.reason}</li>
				{/each}
				{#if shareStatus.rejected.length > 3}<li>and {shareStatus.rejected.length - 3} more</li>{/if}
			</ul>
		{/if}
	{/if}
	{#if !mine && !sharedFrom}
		<p class="-mt-2 mb-2 text-xs text-amber-300/80">
			{teamState.name(sessionState.activeSession.owner_id) || 'Someone else'}'s meeting: you can read, play and export it and tick off its action items, not change it.
		</p>
	{/if}
	{#if sessionState.activeSession.attendees.length}
		<p class="-mt-2 mb-2 text-xs text-gray-500 truncate">Invited: {sessionState.activeSession.attendees.join(', ')}</p>
	{/if}
	<nav class="flex gap-1" aria-label="Meeting">
		{#each tabs as tab}
			<button
				onclick={() => (uiState.activeTab = tab.id)}
				aria-current={uiState.activeTab === tab.id ? 'page' : undefined}
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

{#if publishingState.dialogFor === sessionState.activeSession.id}
	<PublishDialog />
{/if}

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
						<div class="lg:col-span-2 lg:order-2 space-y-4">
							<div class="flex justify-end"><GuestNotesMenu compact /></div>
							<CopilotPanel />
							<section class="rounded-lg border border-gray-800 bg-gray-900/40 p-3 space-y-2" aria-label="Notes">
								<h3 class="text-xs font-semibold uppercase tracking-wide text-gray-400">Notes</h3>
								<NotesEditor maxHeight="max-h-[50vh] overflow-y-auto" />
							</section>
							<ResourcesCard compact />
							<HistoryCard />
						</div>
						<div class="lg:col-span-3 lg:order-1"><LiveTranscript tall /></div>
					</div>
				</div>
			{:else}
				<div class="space-y-4">
					<CopilotPanel />
					{#if mine}
						<CalendarCard />
						<AgendaCard />
						<ResourcesCard />
					{/if}
					{#if sessionState.activeSession.name !== 'Untitled Session' || sessionState.activeSession.attendees.length}
						<MeetingBrief
							title={sessionState.activeSession.name}
							attendees={sessionState.activeSession.attendees}
							exclude={sessionState.activeSession.id}
						/>
					{/if}
					{#if mine}
						{#if audioState.inBrowser}
							<BrowserSources />
						{:else}
							<DeviceSelector />
						{/if}
						<AudioControls
							disabledReason={audioReason(sessionState.activeSession)}
							onStartOverride={() => startRecording()}
							onStopOverride={stopAndTranscribe}
						/>
					{/if}
					{#if mine && !foreign && sessionState.activeSession.audio_file}
						<p class="text-xs text-gray-500">
							Recording again adds to this meeting (part {nextPart}); what is already recorded, transcribed and summarized
							is kept.
						</p>
					{/if}
					<HistoryCard />
					{#if !sharedFrom}
						<RecordsCard />
						<p class="text-xs text-gray-600">
							Shortcuts: <kbd class="px-1 py-0.5 bg-gray-800 rounded text-gray-400">Ctrl+R</kbd> Record
							&middot; <kbd class="px-1 py-0.5 bg-gray-800 rounded text-gray-400">Ctrl+S</kbd> Stop
						</p>
					{/if}
					<LiveTranscript />
				</div>
			{/if}
		{:else if uiState.activeTab === 'transcript'}
			<SupervisionCard />
			<TranscriptView />
		{:else if uiState.activeTab === 'summary'}
			<SummaryView />
		{:else if uiState.activeTab === 'notes'}
			{#if sharedFrom && shareStatus && !shareStatus.parts.notes}
				<p class="text-sm text-gray-500">{contactsState.name(sharedFrom)} didn't share the notes.</p>
			{:else}
				<NotesEditor />
			{/if}
			{#if !sharedFrom}
				<div class="mt-4"><ResourcesCard /></div>
			{/if}
		{:else if uiState.activeTab === 'export'}
			{#if sharedFrom}
				<p class="text-sm text-gray-500">This meeting is {contactsState.name(sharedFrom)}'s: it cannot be exported from here.</p>
			{:else}
				<ObsidianExport />
			{/if}
		{/if}
	</div>
</div>
{/if}
