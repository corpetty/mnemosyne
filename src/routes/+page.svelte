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
	import LiveTranscript from '$lib/components/LiveTranscript.svelte';
	import { getHealth, exportToObsidian } from '$lib/api/backend.js';
	import { wsState } from '$lib/stores/websocket.svelte.js';
	import { transcriptState } from '$lib/stores/transcript.svelte.js';
	import { audioState } from '$lib/stores/audio.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { connectionState } from '$lib/stores/connection.svelte.js';
	import { jobsState } from '$lib/stores/jobs.svelte.js';
	import { askState } from '$lib/stores/ask.svelte.js';
	import AskPanel from '$lib/components/AskPanel.svelte';

	let backendStatus = $state<'checking' | 'connected' | 'unreachable'>('checking');
	// Progress from the Tauri shell while it installs/starts the backend (release builds).
	let shellStage = $state<'installing' | 'starting' | 'ready' | 'error' | null>(null);
	let shellMessage = $state('');
	let shellLog = $state<string[]>([]);
	let showSettings = $state(false);
	let showAsk = $state(false);

	function openAsk() {
		showAsk = true;
		showSettings = false;
		sessionState.activeSession = null;
	}
	let sidebarCollapsed = $state(false);

	// Active tab for session detail view
	type Tab = 'recording' | 'transcript' | 'summary' | 'notes' | 'export';
	let activeTab = $state<Tab>('recording');

	$effect(() => {
		let unsubscribeSessions: (() => void) | null = null;
		let unlistenShell: (() => void) | null = null;
		let cancelled = false;
		let attempts = 0;

		function onConnected() {
			backendStatus = 'connected';
			wsState.connect();
			transcriptState.init();
			jobsState.init();
			askState.init();
			jobsState.onComplete((job) => {
				if (job.kind !== 'summarize' || !job.session_id) return;
				if (sessionState.activeSession?.id === job.session_id) sessionState.refreshActive();
				sessionState.loadSessions();
				toastState.success('Summary ready');
			});
			transcriptState.onComplete((sessionId) => {
				if (sessionState.activeSession?.id === sessionId) sessionState.refreshActive();
				sessionState.loadSessions();
				toastState.success('Transcription complete');
			});
			// Keep the sidebar in step with backend session state changes.
			unsubscribeSessions = wsState.onMessage((msg) => {
				if (msg.type === 'session') sessionState.loadSessions();
			});
		}

		// Poll until the backend answers. In release builds the first launch
		// installs dependencies and can take minutes; the shell reports progress.
		async function connect() {
			while (!cancelled) {
				try {
					const h = await getHealth();
					connectionState.host = h.host ?? null;
					connectionState.authRequired = !!h.auth_required;
					if (!cancelled) onConnected();
					return;
				} catch {
					attempts++;
					if (attempts >= 3 && shellStage !== 'installing' && shellStage !== 'starting') {
						backendStatus = 'unreachable';
					}
					await new Promise((r) => setTimeout(r, 2000));
				}
			}
		}

		// Shell events only exist inside Tauri; ignore when running in a plain browser.
		import('@tauri-apps/api/event')
			.then(({ listen }) =>
				listen<{ stage: typeof shellStage; message: string }>('backend-status', (e) => {
					shellStage = e.payload.stage;
					shellMessage = e.payload.message;
					if (e.payload.stage === 'installing') {
						shellLog = [...shellLog.slice(-7), e.payload.message];
					}
					if (e.payload.stage === 'error') backendStatus = 'unreachable';
				})
			)
			.then((un) => {
				if (cancelled) un();
				else unlistenShell = un;
			})
			.catch(() => {});

		connect();

		return () => {
			cancelled = true;
			unlistenShell?.();
			unsubscribeSessions?.();
			transcriptState.destroy();
			jobsState.destroy();
			askState.destroy();
			wsState.disconnect();
		};
	});

	// Show the active session's transcript when switching sessions
	let lastLoadedSessionId = $state<string | null>(null);
	$effect(() => {
		const session = sessionState.activeSession;
		if (!session) {
			lastLoadedSessionId = null;
			return;
		}
		if (session.id !== lastLoadedSessionId) {
			lastLoadedSessionId = session.id;
			transcriptState.showSession(session.id, session.transcript);
			showSettings = false;
			showAsk = false;
		}
		const pending = sessionState.pendingOpen;
		if (pending && pending.sessionId === session.id) {
			sessionState.pendingOpen = null;
			activeTab = 'transcript';
			if (pending.idx !== null) {
				const idx = pending.idx;
				setTimeout(() => (transcriptState.highlightIndex = idx), 50);
			}
		}
	});

	async function handleStartRecording() {
		if (!sessionState.activeSession) {
			await sessionState.createSession();
		}
		if (sessionState.activeSession) {
			const sessionId = sessionState.activeSession.id;
			const res = await audioState.startRecording(sessionId);
			if (res) {
				transcriptState.startLive(sessionId);
				toastState.info(res.live_job_id ? 'Recording started, live transcript on' : 'Recording started');
				activeTab = 'recording';
			}
		}
	}

	async function handleStopAndTranscribe() {
		const sessionId = audioState.activeSessionId;
		const result = await audioState.stopRecording();
		if (!result || !sessionId) return;
		sessionState.activeSession = result.session;
		if (result.job_id) {
			toastState.info('Transcription queued');
			transcriptState.expectJob(sessionId);
			activeTab = 'transcript';
		} else {
			toastState.info('Recording saved');
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
		} else if (e.ctrlKey && e.key === 'k') {
			e.preventDefault();
			openAsk();
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
				<span class="text-gray-500">{backendStatus === 'connected' ? (connectionState.isLocal ? 'API' : `API @ ${connectionState.host ?? connectionState.url}`) : backendStatus}</span>
			</div>
			{#if wsState.connected}
				<div class="flex items-center gap-1.5">
					<span class="w-2 h-2 rounded-full bg-blue-500"></span>
					<span class="text-gray-500">WS</span>
				</div>
			{/if}
			<button
				onclick={() => (showAsk ? (showAsk = false) : openAsk())}
				title="Ask across all meetings (Ctrl+K)"
				class="px-2.5 py-1 rounded text-xs font-medium transition-colors
					{showAsk ? 'bg-gray-700 text-gray-200' : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800'}"
			>
				Ask
			</button>
			<button
				onclick={() => { showSettings = !showSettings; if (showSettings) { showAsk = false; sessionState.activeSession = null; } }}
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
								<LiveTranscript />
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
			{:else if showAsk}
				<div class="flex-1 overflow-y-auto p-6">
					<div class="max-w-3xl">
						<AskPanel onOpenSession={() => (showAsk = false)} />
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
					<div class="text-center space-y-4">
						<h2 class="text-2xl font-light text-gray-400">Mnemosyne</h2>
						<p class="text-sm text-gray-500">Real-time transcription, diarization, and summarization</p>
						{#if backendStatus === 'checking'}
							<div class="space-y-2">
								<div class="flex items-center justify-center gap-2 text-yellow-500">
									<span class="w-2 h-2 rounded-full bg-yellow-500 animate-pulse"></span>
									<span class="text-sm">
										{#if shellStage === 'installing'}Installing backend (first run){:else if shellStage === 'starting'}Starting backend{:else}Connecting to backend{/if}...
									</span>
								</div>
								{#if shellStage === 'installing' && shellLog.length > 0}
									<pre class="mx-auto max-w-lg text-left text-[11px] leading-4 text-gray-600 bg-gray-900 border border-gray-800 rounded p-2 overflow-hidden whitespace-pre-wrap">{shellLog.join('\n')}</pre>
								{/if}
							</div>
						{:else if backendStatus === 'unreachable'}
							<div class="space-y-2">
								<div class="flex items-center justify-center gap-2 text-red-400">
									<span class="w-2 h-2 rounded-full bg-red-500"></span>
									<span class="text-sm">{shellStage === 'error' ? 'Backend failed to start' : `Backend unreachable at ${connectionState.url}`}</span>
								</div>
								{#if shellStage === 'error'}
									<pre class="mx-auto max-w-lg text-left text-[11px] leading-4 text-red-300/80 bg-gray-900 border border-gray-800 rounded p-2 whitespace-pre-wrap">{shellMessage}</pre>
								{:else}
									<p class="text-xs text-gray-600">Make sure the Python backend is running (still retrying)</p>
								{/if}
							</div>
						{:else}
							<button
								onclick={async () => { await sessionState.createSession(); }}
								class="px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium transition-colors"
							>
								New Session
							</button>
							<p class="text-xs text-gray-600">or select an existing session from the sidebar</p>
						{/if}
						<div class="text-xs text-gray-700 space-y-1 mt-4">
							<p><kbd class="px-1 py-0.5 bg-gray-800 rounded text-gray-500">Ctrl+R</kbd> Start recording</p>
							<p><kbd class="px-1 py-0.5 bg-gray-800 rounded text-gray-500">Ctrl+S</kbd> Stop &amp; transcribe</p>
							<p><kbd class="px-1 py-0.5 bg-gray-800 rounded text-gray-500">Ctrl+E</kbd> Export to Obsidian</p>
							<p><kbd class="px-1 py-0.5 bg-gray-800 rounded text-gray-500">Ctrl+K</kbd> Ask your meetings</p>
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
