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
	import AutoRecordBanner from '$lib/components/AutoRecordBanner.svelte';
	import CalendarBanner from '$lib/components/CalendarBanner.svelte';
	import DigestPanel from '$lib/components/DigestPanel.svelte';
	import HomeView from '$lib/components/HomeView.svelte';
	import PeoplePanel from '$lib/components/PeoplePanel.svelte';
	import SessionList from '$lib/components/SessionList.svelte';
	import SessionView from '$lib/components/SessionView.svelte';
	import SettingsPanel from '$lib/components/SettingsPanel.svelte';
	import SetupWizard from '$lib/components/SetupWizard.svelte';
	import StatusBar from '$lib/components/StatusBar.svelte';
	import TasksPanel from '$lib/components/TasksPanel.svelte';
	import TopicsPanel from '$lib/components/TopicsPanel.svelte';
	import ToastContainer from '$lib/components/ToastContainer.svelte';
	import UpdateBanner from '$lib/components/UpdateBanner.svelte';
	import { audioState } from '$lib/stores/audio.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { transcriptState } from '$lib/stores/transcript.svelte.js';
	import { uiState, type View } from '$lib/stores/ui.svelte.js';
	import type { Component } from 'svelte';

	// Views shown in the main area when no meeting is open.
	const PANELS: Partial<Record<View, { component: Component<{ onOpenSession?: () => void }>; width: string }>> = {
		ask: { component: AskPanel, width: 'max-w-3xl' },
		tasks: { component: TasksPanel, width: 'max-w-3xl' },
		people: { component: PeoplePanel, width: 'max-w-4xl' },
		topics: { component: TopicsPanel, width: 'max-w-3xl' },
		digest: { component: DigestPanel, width: 'max-w-3xl' },
		settings: { component: SettingsPanel as Component<{ onOpenSession?: () => void }>, width: 'max-w-3xl' },
		setup: { component: SetupWizard as Component<{ onOpenSession?: () => void }>, width: 'max-w-2xl' }
	};
	const panel = $derived(PANELS[uiState.view]);

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
	<AutoRecordBanner />
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
			{:else if panel}
				<div class="flex-1 overflow-y-auto p-6">
					<div class={panel.width}>
						<panel.component onOpenSession={() => (uiState.view = 'home')} />
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
