<script lang="ts">
	import DeviceSelector from '$lib/components/DeviceSelector.svelte';
	import AudioControls from '$lib/components/AudioControls.svelte';
	import TranscriptView from '$lib/components/TranscriptView.svelte';
	import SessionList from '$lib/components/SessionList.svelte';
	import NotesEditor from '$lib/components/NotesEditor.svelte';
	import SummaryView from '$lib/components/SummaryView.svelte';
	import ObsidianExport from '$lib/components/ObsidianExport.svelte';
	import SettingsPanel from '$lib/components/SettingsPanel.svelte';
	import ToastContainer from '$lib/components/ToastContainer.svelte';
	import { getHealth } from '$lib/api/backend.js';
	import { exportToObsidian } from '$lib/api/backend.js';
	import { wsState } from '$lib/stores/websocket.svelte.js';
	import { transcriptState } from '$lib/stores/transcript.svelte.js';
	import { audioState } from '$lib/stores/audio.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';

	let backendStatus = $state<'checking' | 'connected' | 'unreachable'>('checking');
	let showSettings = $state(false);
	let sidebarCollapsed = $state(false);

	// Active tab for session detail view
	type Tab = 'recording' | 'transcript' | 'summary' | 'notes' | 'export';
	let activeTab = $state<Tab>('recording');

	$effect(() => {
		getHealth()
			.then(() => {
				backendStatus = 'connected';
				wsState.connect();
				transcriptState.init();
				transcriptState.onComplete(() => {
					sessionState.refreshActive();
					sessionState.loadSessions();
				});
			})
			.catch(() => {
				backendStatus = 'unreachable';
			});

		return () => {
			transcriptState.destroy();
			wsState.disconnect();
		};
	});

	// Load transcript from active session when switching sessions
	let lastLoadedSessionId = $state<string | null>(null);
	$effect(() => {
		const session = sessionState.activeSession;
		if (!session) {
			lastLoadedSessionId = null;
			return;
		}
		if (session.id !== lastLoadedSessionId) {
			lastLoadedSessionId = session.id;
			if (session.transcript.length > 0 && !transcriptState.isProcessing) {
				transcriptState.loadFromSession(session.transcript);
			} else if (session.transcript.length === 0 && !transcriptState.isProcessing) {
				transcriptState.clear();
			}
			showSettings = false;
		}
	});

	async function handleStartRecording() {
		if (!sessionState.activeSession) {
			await sessionState.createSession();
		}
		if (sessionState.activeSession) {
			await audioState.startRecording(sessionState.activeSession.id);
			toastState.info('Recording started');
			activeTab = 'recording';
		}
	}

	async function handleStopAndTranscribe() {
		const result = await audioState.stopRecording();
		if (result?.output_file && result?.session_id) {
			toastState.info('Processing audio...');
			transcriptState.startTranscription(result.output_file, result.session_id);
			activeTab = 'transcript';
		}
	}

	async function handleExportShortcut() {
		if (!sessionState.activeSession) return;
		try {
			const result = await exportToObsidian(sessionState.activeSession.id);
			toastState.success(`Exported to ${result.path}`);
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Export failed');
		}
	}

	function handleKeydown(e: KeyboardEvent) {
		// Ignore when typing in inputs/textareas
		const tag = (e.target as HTMLElement)?.tagName;
		if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;

		if (e.ctrlKey && e.key === 'r') {
			e.preventDefault();
			if (!audioState.isRecording) handleStartRecording();
		} else if (e.ctrlKey && e.key === 's') {
			e.preventDefault();
			if (audioState.isRecording) handleStopAndTranscribe();
		} else if (e.ctrlKey && e.key === 'e') {
			e.preventDefault();
			handleExportShortcut();
		} else if (e.ctrlKey && e.key === 'b') {
			e.preventDefault();
			sidebarCollapsed = !sidebarCollapsed;
		}
	}

	const tabs: { id: Tab; label: string }[] = [
		{ id: 'recording', label: 'Recording' },
		{ id: 'transcript', label: 'Transcript' },
		{ id: 'summary', label: 'Summary' },
		{ id: 'notes', label: 'Notes' },
		{ id: 'export', label: 'Export' }
	];
</script>

<svelte:window onkeydown={handleKeydown} />

<main class="h-screen bg-gray-950 text-gray-100 flex flex-col overflow-hidden">
	<!-- Header -->
	<header class="border-b border-gray-800 px-4 py-2 flex items-center justify-between flex-shrink-0">
		<div class="flex items-center gap-3">
			<button
				onclick={() => (sidebarCollapsed = !sidebarCollapsed)}
				class="p-1 rounded hover:bg-gray-800 text-gray-400 hover:text-gray-200 transition-colors"
				title="Toggle sidebar (Ctrl+B)"
			>
				<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" />
				</svg>
			</button>
			<h1 class="text-lg font-bold tracking-tight">Mnemosyne</h1>
		</div>
		<div class="flex items-center gap-3 text-sm">
			<div class="flex items-center gap-1.5">
				<span class="w-2 h-2 rounded-full {backendStatus === 'connected' ? 'bg-green-500' : backendStatus === 'checking' ? 'bg-yellow-500 animate-pulse' : 'bg-red-500'}"></span>
				<span class="text-gray-500">{backendStatus === 'connected' ? 'API' : backendStatus}</span>
			</div>
			{#if wsState.connected}
				<div class="flex items-center gap-1.5">
					<span class="w-2 h-2 rounded-full bg-blue-500"></span>
					<span class="text-gray-500">WS</span>
				</div>
			{/if}
			<button
				onclick={() => { showSettings = !showSettings; if (showSettings) sessionState.activeSession = null; }}
				class="px-2.5 py-1 rounded text-xs font-medium transition-colors
					{showSettings ? 'bg-gray-700 text-gray-200' : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800'}"
			>
				Settings
			</button>
		</div>
	</header>

	<div class="flex flex-1 overflow-hidden">
		<!-- Sidebar -->
		{#if !sidebarCollapsed}
			<aside class="w-60 border-r border-gray-800 flex flex-col flex-shrink-0">
				<div class="flex-1 overflow-y-auto p-3">
					<SessionList />
				</div>
			</aside>
		{/if}

		<!-- Main content -->
		<div class="flex-1 flex flex-col overflow-hidden">
			{#if sessionState.activeSession}
				<!-- Session header + tabs -->
				<div class="border-b border-gray-800 px-6 pt-4 pb-0 flex-shrink-0">
					<div class="flex items-center justify-between mb-3">
						<h2 class="text-xl font-semibold truncate">{sessionState.activeSession.name}</h2>
						<span class="text-xs text-gray-500 flex-shrink-0">
							{new Date(sessionState.activeSession.created_at).toLocaleString()}
						</span>
					</div>
					<nav class="flex gap-1">
						{#each tabs as tab}
							<button
								onclick={() => (activeTab = tab.id)}
								class="px-3 py-1.5 text-sm rounded-t-lg transition-colors border-b-2
									{activeTab === tab.id
									? 'border-blue-500 text-blue-400 bg-gray-900'
									: 'border-transparent text-gray-500 hover:text-gray-300 hover:bg-gray-900/50'}"
							>
								{tab.label}
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
						{#if activeTab === 'recording'}
							<div class="space-y-4">
								<DeviceSelector />
								<AudioControls
									onStartOverride={handleStartRecording}
									onStopOverride={handleStopAndTranscribe}
								/>
								<p class="text-xs text-gray-600">
									Shortcuts: <kbd class="px-1 py-0.5 bg-gray-800 rounded text-gray-400">Ctrl+R</kbd> Record
									&middot; <kbd class="px-1 py-0.5 bg-gray-800 rounded text-gray-400">Ctrl+S</kbd> Stop
								</p>
							</div>
						{:else if activeTab === 'transcript'}
							<TranscriptView />
						{:else if activeTab === 'summary'}
							<SummaryView />
						{:else if activeTab === 'notes'}
							<NotesEditor />
						{:else if activeTab === 'export'}
							<ObsidianExport />
						{/if}
					</div>
				</div>
			{:else if showSettings}
				<div class="flex-1 overflow-y-auto p-6">
					<div class="max-w-2xl">
						<SettingsPanel />
					</div>
				</div>
			{:else}
				<div class="flex-1 flex items-center justify-center text-gray-600">
					<div class="text-center space-y-3">
						<h2 class="text-2xl font-light">Mnemosyne</h2>
						<p class="text-sm">Select or create a session to get started</p>
						<div class="text-xs text-gray-700 space-y-1 mt-4">
							<p><kbd class="px-1 py-0.5 bg-gray-800 rounded text-gray-500">Ctrl+R</kbd> Start recording</p>
							<p><kbd class="px-1 py-0.5 bg-gray-800 rounded text-gray-500">Ctrl+S</kbd> Stop &amp; transcribe</p>
							<p><kbd class="px-1 py-0.5 bg-gray-800 rounded text-gray-500">Ctrl+E</kbd> Export to Obsidian</p>
							<p><kbd class="px-1 py-0.5 bg-gray-800 rounded text-gray-500">Ctrl+B</kbd> Toggle sidebar</p>
						</div>
					</div>
				</div>
			{/if}
		</div>
	</div>

	<!-- Status bar -->
	<footer class="border-t border-gray-800 px-4 py-1 flex items-center justify-between text-xs text-gray-600 flex-shrink-0">
		<div class="flex items-center gap-4">
			{#if audioState.isRecording}
				<div class="flex items-center gap-1.5 text-red-400">
					<span class="w-1.5 h-1.5 rounded-full bg-red-500 animate-pulse"></span>
					Recording
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
		</div>
		<div class="flex items-center gap-3">
			<span>{sessionState.sessions.length} session{sessionState.sessions.length !== 1 ? 's' : ''}</span>
		</div>
	</footer>
</main>

<ToastContainer />
