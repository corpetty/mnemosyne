<script lang="ts">
	import {
		checkForUpdateOnce,
		connectApp,
		handleKeydown,
		invokeShell,
		listenForRemoteActions,
		runLaunchActionOnce
	} from '$lib/app/controller.svelte.js';
	import AppHeader from '$lib/components/AppHeader.svelte';
	import AskPanel from '$lib/components/AskPanel.svelte';
	import CalendarBanner from '$lib/components/CalendarBanner.svelte';
	import DigestPanel from '$lib/components/DigestPanel.svelte';
	import HomeView from '$lib/components/HomeView.svelte';
	import PeoplePanel from '$lib/components/PeoplePanel.svelte';
	import SessionList from '$lib/components/SessionList.svelte';
	import SessionView from '$lib/components/SessionView.svelte';
	import SettingsPanel from '$lib/components/SettingsPanel.svelte';
	import StatusBar from '$lib/components/StatusBar.svelte';
	import TasksPanel from '$lib/components/TasksPanel.svelte';
	import TopicsPanel from '$lib/components/TopicsPanel.svelte';
	import ToastContainer from '$lib/components/ToastContainer.svelte';
	import UpdateBanner from '$lib/components/UpdateBanner.svelte';
	import { audioState } from '$lib/stores/audio.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { transcriptState } from '$lib/stores/transcript.svelte.js';
	import { uiState } from '$lib/stores/ui.svelte.js';

	$effect(() => connectApp());
	$effect(() => listenForRemoteActions());
	$effect(() => runLaunchActionOnce());
	$effect(() => checkForUpdateOnce());

	// Mirror recording state into the tray label and tooltip.
	$effect(() => {
		invokeShell('set_recording_state', { recording: audioState.isRecording });
	});

	// Show the active session's transcript when switching sessions; honour search/citation jumps.
	let lastLoadedSessionId: string | null = null;
	$effect(() => {
		const session = sessionState.activeSession;
		if (!session) {
			lastLoadedSessionId = null;
			return;
		}
		if (session.id !== lastLoadedSessionId) {
			lastLoadedSessionId = session.id;
			transcriptState.showSession(session.id, session.transcript);
			uiState.closePanels();
		}
		const pending = sessionState.pendingOpen;
		if (pending && pending.sessionId === session.id) {
			sessionState.pendingOpen = null;
			uiState.activeTab = 'transcript';
			if (pending.idx !== null) {
				const idx = pending.idx;
				setTimeout(() => (transcriptState.highlightIndex = idx), 50);
			}
		}
	});
</script>

<svelte:window onkeydown={handleKeydown} />

<main class="h-screen bg-gray-950 text-gray-100 flex flex-col overflow-hidden">
	<AppHeader />
	<UpdateBanner />
	<CalendarBanner />

	<div class="flex flex-1 overflow-hidden">
		{#if !uiState.sidebarCollapsed}
			<aside class="w-60 border-r border-gray-800 flex flex-col flex-shrink-0">
				<div class="flex-1 overflow-y-auto p-3">
					<SessionList />
				</div>
			</aside>
		{/if}

		<div class="flex-1 flex flex-col overflow-hidden">
			{#if sessionState.activeSession}
				<SessionView />
			{:else if uiState.showAsk}
				<div class="flex-1 overflow-y-auto p-6">
					<div class="max-w-3xl">
						<AskPanel onOpenSession={() => (uiState.showAsk = false)} />
					</div>
				</div>
			{:else if uiState.showPeople}
				<div class="flex-1 overflow-y-auto p-6">
					<div class="max-w-4xl">
						<PeoplePanel onOpenSession={() => (uiState.showPeople = false)} />
					</div>
				</div>
			{:else if uiState.showTopics}
				<div class="flex-1 overflow-y-auto p-6">
					<div class="max-w-3xl">
						<TopicsPanel onOpenSession={() => (uiState.showTopics = false)} />
					</div>
				</div>
			{:else if uiState.showTasks}
				<div class="flex-1 overflow-y-auto p-6">
					<div class="max-w-3xl">
						<TasksPanel onOpenSession={() => (uiState.showTasks = false)} />
					</div>
				</div>
			{:else if uiState.showDigest}
				<div class="flex-1 overflow-y-auto p-6">
					<div class="max-w-3xl">
						<DigestPanel onOpenSession={() => (uiState.showDigest = false)} />
					</div>
				</div>
			{:else if uiState.showSettings}
				<div class="flex-1 overflow-y-auto p-6">
					<div class="max-w-2xl">
						<SettingsPanel />
					</div>
				</div>
			{:else}
				<HomeView />
			{/if}
		</div>
	</div>

	<StatusBar />
</main>

<ToastContainer />
