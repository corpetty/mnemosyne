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
	import CommandPalette from '$lib/components/CommandPalette.svelte';
	import ConsentDialog from '$lib/components/ConsentDialog.svelte';
	import DigestPanel from '$lib/components/DigestPanel.svelte';
	import HomeView from '$lib/components/HomeView.svelte';
	import PeoplePanel from '$lib/components/PeoplePanel.svelte';
	import QuitDialog from '$lib/components/QuitDialog.svelte';
	import SatelliteBanner from '$lib/components/SatelliteBanner.svelte';
	import SessionList from '$lib/components/SessionList.svelte';
	import SessionView from '$lib/components/SessionView.svelte';
	import SettingsPanel from '$lib/components/SettingsPanel.svelte';
	import SignInScreen from '$lib/components/SignInScreen.svelte';
	import SupervisionPanel from '$lib/components/SupervisionPanel.svelte';
	import SetupWizard from '$lib/components/SetupWizard.svelte';
	import StatusBar from '$lib/components/StatusBar.svelte';
	import TasksPanel from '$lib/components/TasksPanel.svelte';
	import TopicsPanel from '$lib/components/TopicsPanel.svelte';
	import ToastContainer from '$lib/components/ToastContainer.svelte';
	import UpdateBanner from '$lib/components/UpdateBanner.svelte';
	import UnlockScreen from '$lib/components/UnlockScreen.svelte';
	import { audioState } from '$lib/stores/audio.svelte.js';
	import { connectionState } from '$lib/stores/connection.svelte.js';
	import { jobsState } from '$lib/stores/jobs.svelte.js';
	import { satelliteState } from '$lib/stores/satellite.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { teamMemberState } from '$lib/stores/teamMember.svelte.js';
	import { transcriptState } from '$lib/stores/transcript.svelte.js';
	import { uiState, type View } from '$lib/stores/ui.svelte.js';
	import { untrack, type Component } from 'svelte';

	// Views shown in the main area when no meeting is open.
	const PANELS: Partial<Record<View, { component: Component<{ onOpenSession?: () => void }>; width: string }>> = {
		ask: { component: AskPanel, width: 'max-w-4xl' },
		tasks: { component: TasksPanel, width: 'max-w-5xl' },
		people: { component: PeoplePanel, width: 'max-w-5xl' },
		topics: { component: TopicsPanel, width: 'max-w-5xl' },
		digest: { component: DigestPanel, width: 'max-w-4xl' },
		review: { component: SupervisionPanel, width: 'max-w-5xl' },
		settings: { component: SettingsPanel as Component<{ onOpenSession?: () => void }>, width: 'max-w-3xl' },
		setup: { component: SetupWizard as Component<{ onOpenSession?: () => void }>, width: 'max-w-2xl' }
	};
	const panel = $derived(PANELS[uiState.view]);
	// One scroller for every view and every Settings tab: back to the top on a change, else the
	// next one opened at the last one's offset (cut off, or at its very bottom).
	let panelScroller = $state<HTMLDivElement | null>(null);
	$effect.pre(() => {
		void uiState.view;
		void uiState.settingsTab;
		if (panelScroller) panelScroller.scrollTop = 0;
	});

	$effect(() => connectApp());
	$effect(() => listenForRemoteActions());
	$effect(() => runLaunchActionOnce());
	$effect(() => checkForUpdateOnce());
	// A computer that uses a main computer asks its own backend what it is recording and what waits.
	$effect(() => {
		const uses = connectionState.usesMain;
		untrack(() => satelliteState.follow(uses));
		return () => satelliteState.stop();
	});

	// Whether this computer has joined a team (and can reach it) comes from its own backend, which the
	// window may reach even when the team's library does not answer.
	$effect(() => {
		void teamMemberState.load();
		return () => teamMemberState.stop();
	});

	// Mirror recording state into the tray label and tooltip.
	$effect(() => {
		invokeShell('set_recording_state', { recording: audioState.isRecording });
	});

	// Show the active session's transcript when switching sessions; honour search/citation jumps.
	let lastLoadedSessionId: string | null = null;
	let lastTranscript: unknown = null;
	$effect(() => {
		const session = sessionState.activeSession;
		if (!session) {
			lastLoadedSessionId = null;
			return;
		}
		if (session.id !== lastLoadedSessionId) {
			lastLoadedSessionId = session.id;
			const running = untrack(() =>
				Object.values(jobsState.jobs).find(
					(j) =>
						j.kind === 'transcribe' &&
						j.session_id === session.id &&
						(j.status === 'queued' || j.status === 'running')
				)
			);
			transcriptState.showSession(session.id, session.transcript, running);
			uiState.closePanels();
			// A finished meeting opens on what it produced, not on the recording tab left
			// from the last one (the tab chosen otherwise stays as it was).
			const recordingIt = untrack(() => audioState.isRecording && audioState.activeSessionId === session.id);
			if (untrack(() => uiState.activeTab) === 'recording' && !recordingIt) {
				if (session.summary) uiState.activeTab = 'summary';
				else if (session.transcript.length) uiState.activeTab = 'transcript';
			}
		} else if (session.transcript !== lastTranscript) {
			// The same meeting, reloaded with a changed transcript (glossary applied, meetings
			// combined, speakers renamed elsewhere): show it, unless a transcription is running.
			transcriptState.showSession(session.id, session.transcript);
		}
		lastTranscript = session.transcript;
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
	<SatelliteBanner />

	{#if uiState.locked}
		<UnlockScreen />
	{:else if uiState.signIn}
		<SignInScreen />
	{:else}
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
					<div bind:this={panelScroller} class="flex-1 overflow-y-auto p-6">
						<div class="mx-auto w-full {panel.width}">
							<panel.component onOpenSession={() => (uiState.view = 'home')} />
						</div>
					</div>
				{:else}
					<HomeView />
				{/if}
			</div>
		</div>
	{/if}

	<StatusBar />
</main>

{#if uiState.paletteOpen}
	<CommandPalette />
{/if}
{#if uiState.quitAsk}
	<QuitDialog />
{/if}
{#if uiState.consentAsk}
	<ConsentDialog />
{/if}
<ToastContainer />
