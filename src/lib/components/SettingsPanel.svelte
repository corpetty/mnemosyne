<script lang="ts">
	import { deleteSpeakerProfile, getSettings, listModels, listSpeakers, renameSpeakerProfile, updateSettings } from '$lib/api/backend.js';
	import { loadAutoRecordSettings, openSetup } from '$lib/app/controller.svelte.js';
	import { updateState } from '$lib/stores/update.svelte.js';
	import { uiState } from '$lib/stores/ui.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { connectionState, LOCAL_BACKEND } from '$lib/stores/connection.svelte.js';
	import PhoneLink from './PhoneLink.svelte';
	import StorageSettings from './StorageSettings.svelte';
	import { calendarState } from '$lib/stores/calendar.svelte.js';
	import { checkGitHub, checkIntegration, getDiagnostics, getIndexStatus, rebuildIndex } from '$lib/api/backend.js';
	import type { IndexStatus, ProviderModels, SettingsResponse, SettingsUpdate, SpeakerProfile } from '$lib/types/index.js';

	let settings = $state<SettingsResponse | null>(null);
	let providers = $state<ProviderModels[]>([]);
	let voices = $state<SpeakerProfile[]>([]);
	let loading = $state(false);
	let saving = $state(false);
	let error = $state('');

	// Editable copy of the values
	let form = $state<SettingsUpdate>({});
	// Secret inputs are separate: '' = keep current, text = replace
	type SecretKey =
		| 'hf_token'
		| 'openai_api_key'
		| 'anthropic_api_key'
		| 'remote_stt_api_key'
		| 'api_token'
		| 'calendar_ics_url'
		| 'github_token'
		| 'linear_api_key'
		| 'jira_api_token'
		| 'slack_webhook_url'
		| 'matrix_access_token';
	const SECRET_KEYS: SecretKey[] = [
		'hf_token',
		'openai_api_key',
		'anthropic_api_key',
		'remote_stt_api_key',
		'api_token',
		'calendar_ics_url',
		'github_token',
		'linear_api_key',
		'jira_api_token',
		'slack_webhook_url',
		'matrix_access_token'
	];
	const emptySecrets = (): Record<SecretKey, string> => ({
		hf_token: '',
		openai_api_key: '',
		anthropic_api_key: '',
		remote_stt_api_key: '',
		api_token: '',
		calendar_ics_url: '',
		github_token: '',
		linear_api_key: '',
		jira_api_token: '',
		slack_webhook_url: '',
		matrix_access_token: ''
	});
	let connUrl = $state(connectionState.url);
	let connToken = $state(connectionState.token);

	function applyConnection(url: string, token: string) {
		connectionState.save(url, token);
		location.reload();
	}
	let secrets = $state<Record<SecretKey, string>>(emptySecrets());

	const WHISPER_MODELS = ['tiny', 'base', 'small', 'medium', 'medium.en', 'large-v2', 'large-v3', 'large-v3-turbo'];
	const COMPUTE_TYPES = ['float16', 'int8', 'float32'];

	function locked(field: string): boolean {
		return settings?.env_overrides.includes(field) ?? false;
	}

	async function load() {
		loading = true;
		error = '';
		try {
			settings = await getSettings();
			const v = settings.values;
			form = {
				transcriber: v.transcriber,
				diarizer: v.diarizer,
				language: v.language,
				max_speakers: v.max_speakers,
				local_speaker_name: v.local_speaker_name,
				remote_speaker_name: v.remote_speaker_name,
				echo_dedup: v.echo_dedup,
				echo_similarity: v.echo_similarity,
				auto_label_speakers: v.auto_label_speakers,
				speaker_match_threshold: v.speaker_match_threshold,
				per_source_transcription: v.per_source_transcription,
				live_transcription: v.live_transcription,
				live_transcriber: v.live_transcriber,
				live_interval_seconds: v.live_interval_seconds,
				live_diarization: v.live_diarization,
				live_speaker_threshold: v.live_speaker_threshold,
				mention_keywords: v.mention_keywords,
				live_threads: v.live_threads,
				live_adaptive: v.live_adaptive,
				copilot: v.copilot,
				copilot_interval_seconds: v.copilot_interval_seconds,
				whisper_model_size: v.whisper_model_size,
				whisper_compute_type: v.whisper_compute_type,
				whisper_batch_size: v.whisper_batch_size,
				parakeet_model: v.parakeet_model,
				parakeet_quantization: v.parakeet_quantization,
				onnx_provider: v.onnx_provider,
				remote_stt_url: v.remote_stt_url,
				remote_stt_model: v.remote_stt_model,
				diarization_model: v.diarization_model,
				auto_transcribe: v.auto_transcribe,
				glossary: v.glossary,
				glossary_llm_correct: v.glossary_llm_correct,
				auto_summarize: v.auto_summarize,
				auto_name_sessions: v.auto_name_sessions,
				calendar_auto_name: v.calendar_auto_name,
				github_repo: v.github_repo,
				github_labels: v.github_labels,
				linear_team: v.linear_team,
				jira_url: v.jira_url,
				jira_email: v.jira_email,
				jira_project: v.jira_project,
				jira_issue_type: v.jira_issue_type,
				matrix_homeserver: v.matrix_homeserver,
				matrix_room_id: v.matrix_room_id,
				ollama_url: v.ollama_url,
				vllm_url: v.vllm_url,
				default_provider: v.default_provider,
				default_model: v.default_model,
				cloud_redaction: v.cloud_redaction,
				auto_record: v.auto_record,
				auto_stop_silence_minutes: v.auto_stop_silence_minutes,
				auto_record_ignore_apps: v.auto_record_ignore_apps,
				summary_style: v.summary_style,
				summary_instructions: v.summary_instructions,
				summary_chunk_chars: v.summary_chunk_chars,
				semantic_search: v.semantic_search,
				embedding_model: v.embedding_model,
				obsidian_tags: v.obsidian_tags,
				obsidian_link_people: v.obsidian_link_people,
				obsidian_include_transcript: v.obsidian_include_transcript,
				obsidian_auto_export: v.obsidian_auto_export,
				obsidian_people_notes: v.obsidian_people_notes,
				digest_weekday: v.digest_weekday,
				digest_hour: v.digest_hour
			};
			providers = await listModels();
			voices = await listSpeakers();
			updateState.probe();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load settings';
		} finally {
			loading = false;
		}
	}

	let indexStatus = $state<IndexStatus | null>(null);
	async function refreshIndexStatus() {
		try {
			indexStatus = await getIndexStatus();
		} catch {
			indexStatus = null;
		}
	}
	async function handleRebuild() {
		indexStatus = await rebuildIndex();
		toastState.info('Re-indexing all meetings in the background');
	}
	$effect(() => {
		refreshIndexStatus();
		const t = setInterval(refreshIndexStatus, 5000);
		return () => clearInterval(t);
	});

	async function save() {
		saving = true;
		error = '';
		try {
			const update: SettingsUpdate = { ...form };
			for (const key of SECRET_KEYS) {
				if (secrets[key] !== '') update[key] = secrets[key];
			}
			settings = await updateSettings(update);
			loadAutoRecordSettings();
			secrets = emptySecrets();
			providers = await listModels();
			toastState.success('Settings saved');
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to save settings';
		} finally {
			saving = false;
		}
	}

	let copyingDiagnostics = $state(false);
	let logFile = $state('');

	/** Versions, engines, recent errors, settings without secrets and the backend log tail. */
	async function copyDiagnostics() {
		copyingDiagnostics = true;
		try {
			const d = await getDiagnostics();
			logFile = d.log_file;
			let app = '';
			try {
				const { getVersion } = await import('@tauri-apps/api/app');
				app = `app ${await getVersion()}\n`;
			} catch {
				app = `browser ${navigator.userAgent}\n`;
			}
			await navigator.clipboard.writeText(app + d.text);
			toastState.success('Diagnostics copied; read them before posting publicly');
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not collect diagnostics');
		} finally {
			copyingDiagnostics = false;
		}
	}

	let calTest = $state<string | null>(null);
	async function testCalendar() {
		calTest = 'Checking…';
		await calendarState.refresh(true);
		calTest = !calendarState.configured
			? 'No calendar configured (save first).'
			: calendarState.error
				? `Error: ${calendarState.error}`
				: `OK: ${calendarState.upcoming.length} meeting(s) in the next 12 hours${calendarState.current ? `, now: ${calendarState.current.title}` : ''}.`;
	}

	let integrationTest = $state<Record<string, string>>({});
	async function testIntegration(name: 'linear' | 'jira' | 'slack' | 'matrix') {
		integrationTest = { ...integrationTest, [name]: 'Checking…' };
		try {
			const r = await checkIntegration(name);
			integrationTest = { ...integrationTest, [name]: `${r.ok ? '✓' : '✗'} ${r.message}` };
		} catch (e) {
			integrationTest = { ...integrationTest, [name]: e instanceof Error ? e.message : 'Check failed' };
		}
	}

	let ghTest = $state<string | null>(null);
	async function testGitHub() {
		ghTest = 'Checking…';
		try {
			const r = await checkGitHub();
			ghTest = `${r.ok ? '✓' : '✗'} ${r.message}`;
		} catch (e) {
			ghTest = e instanceof Error ? e.message : 'Check failed';
		}
	}

	async function clearSecret(key: SecretKey) {
		try {
			settings = await updateSettings({ [key]: null });
			providers = await listModels();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to clear';
		}
	}

	$effect(() => {
		load();
	});

	async function renameVoice(v: SpeakerProfile) {
		const name = prompt('Rename speaker', v.name);
		if (!name || name === v.name) return;
		try {
			await renameSpeakerProfile(v.id, name);
			voices = await listSpeakers();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Rename failed';
		}
	}

	async function forgetVoice(v: SpeakerProfile) {
		if (!confirm(`Forget ${v.name}'s voice? Existing transcripts keep the name.`)) return;
		await deleteSpeakerProfile(v.id);
		voices = await listSpeakers();
	}

	const TABS = [
		{ id: 'general', label: 'General' },
		{ id: 'recording', label: 'Recording' },
		{ id: 'transcription', label: 'Transcription' },
		{ id: 'ai', label: 'AI' },
		{ id: 'notes', label: 'Notes & sharing' }
	] as const;
	const tab = $derived(uiState.settingsTab);

	const inputClass =
		'bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 disabled:opacity-60 w-full';
	const labelClass = 'block text-xs text-gray-500 mb-1';
</script>

<div class="space-y-6">
	<nav class="flex flex-wrap gap-1 border-b border-gray-800 pb-2" aria-label="Settings sections">
		{#each TABS as t (t.id)}
			<button
				onclick={() => (uiState.settingsTab = t.id)}
				aria-current={tab === t.id ? 'page' : undefined}
				class="px-3 py-1 text-sm rounded {tab === t.id ? 'bg-gray-700 text-gray-100' : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800'}"
			>{t.label}</button>
		{/each}
		<button onclick={openSetup} class="ml-auto px-3 py-1 text-xs rounded text-gray-500 hover:text-gray-300">Run setup again</button>
	</nav>

	{#if tab === 'general'}
		<!-- Connection (stored in this app instance, not on the backend) -->
		<section>
			<h3 class="text-lg font-semibold text-gray-200 mb-1">Connection</h3>
			<p class="text-xs text-gray-500 mb-3">
				Which backend this window talks to. Recording happens where the backend runs; a remote backend is for
				working with sessions on another machine (e.g. a GPU box). Currently:
				<code class="text-gray-400">{connectionState.url}</code>{#if connectionState.host} on <span class="text-gray-300">{connectionState.host}</span>{/if}.
			</p>
			<div class="grid grid-cols-2 gap-3">
				<label>
					<span class={labelClass}>Backend URL</span>
					<input type="text" bind:value={connUrl} placeholder={LOCAL_BACKEND} class={inputClass} />
				</label>
				<label>
					<span class={labelClass}>API token (if the backend requires one)</span>
					<input type="password" bind:value={connToken} placeholder="optional" class={inputClass} />
				</label>
			</div>
			<div class="flex items-center gap-3 mt-2">
				<button onclick={() => applyConnection(connUrl, connToken)} class="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300">Connect</button>
				{#if !connectionState.isLocal}
					<button onclick={() => applyConnection(LOCAL_BACKEND, '')} class="text-sm text-gray-400 hover:text-gray-200">Use local backend</button>
				{/if}
			</div>
		</section>

		<section>
			<h3 class="text-lg font-semibold text-gray-200 mb-1">Troubleshooting</h3>
			<p class="text-xs text-gray-500 mb-3">
				Copies versions, GPU, engines, recent errors, your settings (keys, names and addresses left out) and the end of
				the backend log, ready to paste into an issue. Read it before posting publicly: the log can mention meeting names.
			</p>
			<div class="flex flex-wrap items-center gap-3">
				<button
					onclick={copyDiagnostics}
					disabled={copyingDiagnostics}
					class="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 disabled:opacity-50"
				>{copyingDiagnostics ? 'Collecting…' : 'Copy diagnostics'}</button>
				{#if logFile}
					<span class="text-xs text-gray-500">Backend log: <code class="text-gray-400 select-all">{logFile}</code></span>
				{/if}
			</div>
		</section>
	{/if}

	{#if loading && !settings}
		<p class="text-gray-500 text-sm">Loading settings...</p>
	{:else if settings}
		{#if tab === 'recording'}
			<!-- Auto-record -->
			<section>
				<h3 class="text-lg font-semibold text-gray-200 mb-1">Auto-record</h3>
				<p class="text-xs text-gray-500 mb-3">
					Notice when a meeting starts: an app (Zoom, Teams, a browser call…) starts using the microphone, or a calendar meeting begins.
				</p>
				<div class="grid grid-cols-2 gap-3">
					<label>
						<span class={labelClass}>When a meeting starts</span>
						<select bind:value={form.auto_record} disabled={locked('auto_record')} class={inputClass}>
							<option value="off">Do nothing</option>
							<option value="ask">Ask me</option>
							<option value="auto">Start recording</option>
						</select>
					</label>
					<label>
						<span class={labelClass}>Stop after this many silent minutes (0 = never)</span>
						<input type="number" min="0" max="120" bind:value={form.auto_stop_silence_minutes} disabled={locked('auto_stop_silence_minutes')} class={inputClass} />
					</label>
					<label class="col-span-2">
						<span class={labelClass}>Ignore these apps (comma-separated)</span>
						<input type="text" bind:value={form.auto_record_ignore_apps} disabled={locked('auto_record_ignore_apps')} class={inputClass} />
					</label>
					<p class="col-span-2 text-[11px] text-gray-600 -mt-1">
						Recordings started automatically stop a minute after the app stops using the microphone, 5 minutes after the calendar meeting ends, or after the silence above. They use the devices you last selected; the app must be open.
					</p>
				</div>
			</section>

			<!-- Live -->
			<section>
				<h3 class="text-lg font-semibold text-gray-200 mb-1">Live transcript</h3>
				<p class="text-xs text-gray-500 mb-3">Provisional text while recording, a few seconds behind speech. The final transcription replaces it.</p>
				<div class="grid grid-cols-2 gap-3">
					<label class="flex items-center gap-2 col-span-2">
						<input type="checkbox" bind:checked={form.live_transcription} disabled={locked('live_transcription')} class="rounded border-gray-600 bg-gray-800" />
						<span class="text-sm text-gray-300">Show live transcript while recording</span>
					</label>
					<label>
						<span class={labelClass}>Live transcriber</span>
						<select bind:value={form.live_transcriber} disabled={locked('live_transcriber')} class={inputClass}>
							<option value="parakeet">Parakeet (ONNX, CPU, recommended)</option>
							<option value="remote">Remote server</option>
							<option value="whisperx">WhisperX (GPU, competes with final jobs)</option>
						</select>
					</label>
					<label>
						<span class={labelClass}>Update interval (seconds)</span>
						<input type="number" min="2" max="30" step="1" bind:value={form.live_interval_seconds} disabled={locked('live_interval_seconds')} class={inputClass} />
					</label>
					<label class="flex items-center gap-2">
						<input type="checkbox" bind:checked={form.live_diarization} disabled={locked('live_diarization') || form.diarizer === 'none'} class="rounded border-gray-600 bg-gray-800" />
						<span class="text-sm text-gray-300">Tell speakers apart live (known voices are named)</span>
					</label>
					<label>
						<span class={labelClass}>Live speaker separation (0.35 merges more … 0.6 splits more)</span>
						<input type="number" min="0.2" max="0.9" step="0.05" bind:value={form.live_speaker_threshold} disabled={locked('live_speaker_threshold') || !form.live_diarization} class={inputClass} />
					</label>
					<label>
						<span class={labelClass}>CPU threads for the live transcript (more is not faster; all cores freezes the desktop)</span>
						<input type="number" min="1" max="8" bind:value={form.live_threads} disabled={locked('live_threads') || !form.live_transcription} class={inputClass} />
					</label>
					<label class="flex items-center gap-2">
						<input type="checkbox" bind:checked={form.live_adaptive} disabled={locked('live_adaptive') || !form.live_transcription} class="rounded border-gray-600 bg-gray-800" />
						<span class="text-sm text-gray-300">Slow the live transcript down when it falls behind or the computer is busy</span>
					</label>
					<label>
						<span class={labelClass}>Label for the system-audio channel</span>
						<input type="text" bind:value={form.remote_speaker_name} disabled={locked('remote_speaker_name')} class={inputClass} />
					</label>
					<label class="flex items-center gap-2 col-span-2">
						<input type="checkbox" bind:checked={form.copilot} disabled={locked('copilot') || !form.live_transcription} class="rounded border-gray-600 bg-gray-800" />
						<span class="text-sm text-gray-300">Copilot: keep running notes (so far, decided, to do, open) while recording, with the default model</span>
					</label>
					<label>
						<span class={labelClass}>Refresh the notes at most every (seconds)</span>
						<input type="number" min="30" step="30" bind:value={form.copilot_interval_seconds} disabled={locked('copilot_interval_seconds') || !form.copilot} class={inputClass} />
					</label>
					<label class="col-span-2">
						<span class={labelClass}>Alert me when these are said while recording (comma-separated, e.g. your name)</span>
						<input type="text" bind:value={form.mention_keywords} disabled={locked('mention_keywords')} placeholder="Corey, infra team" class={inputClass} />
						<span class="block text-[11px] text-gray-600 mt-1">A toast and a desktop notification, from anyone but your own mic. Needs the live transcript.</span>
					</label>
				</div>
			</section>

			<!-- Calendar -->
			<section>
				<h3 class="text-lg font-semibold text-gray-200 mb-1">Calendar</h3>
				<p class="text-xs text-gray-500 mb-3">
					Paste your calendar's private ICS address (Google: Settings → your calendar → "Secret address in iCal format"; Fastmail, Proton, Outlook and Nextcloud have the same). Recordings started during a meeting are named after it, and its invitees are offered when you name speakers. A local .ics path also works.
				</p>
				<div class="grid grid-cols-2 gap-3">
					<label class="col-span-2">
						<span class={labelClass}>
							ICS address
							{#if settings.secrets_set.calendar_ics_url}<span class="text-green-500 ml-1">set</span>{/if}
						</span>
						<div class="flex gap-2">
							<input type="password" bind:value={secrets.calendar_ics_url} disabled={locked('calendar_ics_url')} placeholder={settings.secrets_set.calendar_ics_url ? '•••••••• (leave blank to keep)' : 'https://calendar.google.com/calendar/ical/.../basic.ics'} class={inputClass} />
							{#if settings.secrets_set.calendar_ics_url && !locked('calendar_ics_url')}
								<button onclick={() => clearSecret('calendar_ics_url')} class="text-xs text-gray-500 hover:text-red-400">Clear</button>
							{/if}
						</div>
					</label>
					<label class="flex items-center gap-2">
						<input type="checkbox" bind:checked={form.calendar_auto_name} disabled={locked('calendar_auto_name')} class="rounded border-gray-600 bg-gray-800" />
						<span class="text-sm text-gray-300">Name new recordings after the current meeting</span>
					</label>
					<div class="flex items-center gap-2">
						<button onclick={testCalendar} class="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300">Test</button>
						{#if calTest}<span class="text-xs text-gray-400">{calTest}</span>{/if}
					</div>
				</div>
			</section>

			<!-- Speakers -->
			<section>
				<h3 class="text-lg font-semibold text-gray-200 mb-1">Speakers</h3>
				<p class="text-xs text-gray-500 mb-3">Rename a speaker in a transcript to teach the app their voice; matching voices are labelled automatically afterwards.</p>
				<div class="grid grid-cols-2 gap-3">
					<label class="flex items-center gap-2">
						<input type="checkbox" bind:checked={form.auto_label_speakers} disabled={locked('auto_label_speakers')} class="rounded border-gray-600 bg-gray-800" />
						<span class="text-sm text-gray-300">Label known voices automatically</span>
					</label>
					<label>
						<span class={labelClass}>Voice match threshold (cosine, 0.4 loose … 0.8 strict)</span>
						<input type="number" min="0.3" max="0.95" step="0.05" bind:value={form.speaker_match_threshold} disabled={locked('speaker_match_threshold')} class={inputClass} />
					</label>
					<label class="flex items-center gap-2">
						<input type="checkbox" bind:checked={form.echo_dedup} disabled={locked('echo_dedup')} class="rounded border-gray-600 bg-gray-800" />
						<span class="text-sm text-gray-300">Drop mic segments that repeat the speakers (no headphones)</span>
					</label>
					<label>
						<span class={labelClass}>Echo text similarity (0.6 aggressive … 0.95 strict)</span>
						<input type="number" min="0.5" max="1" step="0.05" bind:value={form.echo_similarity} disabled={locked('echo_similarity')} class={inputClass} />
					</label>
				</div>
				<div class="mt-3">
					<h4 class="text-sm font-semibold text-gray-300 mb-1">Known voices</h4>
					{#if voices.length === 0}
						<p class="text-xs text-gray-600">None yet. Rename a speaker in a transcript with "remember voice" ticked.</p>
					{:else}
						<div class="flex flex-wrap gap-2">
							{#each voices as v (v.id)}
								<div class="flex items-center gap-2 bg-gray-900 border border-gray-700 rounded px-2 py-1 text-xs">
									<span class="text-gray-200 font-medium">{v.name}</span>
									<span class="text-gray-600">{v.sample_count} sample{v.sample_count !== 1 ? 's' : ''}</span>
									<button onclick={() => renameVoice(v)} class="text-gray-500 hover:text-gray-200">rename</button>
									<button onclick={() => forgetVoice(v)} class="text-gray-500 hover:text-red-400">forget</button>
								</div>
							{/each}
						</div>
					{/if}
				</div>
			</section>

			<div class="flex items-center gap-3">
				<button
					onclick={save}
					disabled={saving}
					class="px-4 py-1.5 text-sm rounded bg-blue-600 hover:bg-blue-700 disabled:bg-gray-700 text-white font-medium transition-colors"
				>
					{saving ? 'Saving...' : 'Save settings'}
				</button>
				<button onclick={load} disabled={loading} class="text-sm text-gray-400 hover:text-gray-200">Reload</button>
				{#if error}<span class="text-sm text-red-400">{error}</span>{/if}
			</div>
		{/if}

		{#if tab === 'transcription'}
			<!-- Transcription -->
			<section>
				<h3 class="text-lg font-semibold text-gray-200 mb-3">Transcription</h3>
				<div class="grid grid-cols-2 gap-3">
					<label>
						<span class={labelClass}>Transcriber</span>
						<select bind:value={form.transcriber} disabled={locked('transcriber')} class={inputClass}>
							<option value="whisperx">WhisperX (GPU, torch)</option>
							<option value="parakeet">Parakeet TDT (ONNX, CPU/GPU)</option>
							<option value="remote">Remote OpenAI-compatible server</option>
						</select>
					</label>
					<label>
						<span class={labelClass}>Diarizer</span>
						<select bind:value={form.diarizer} disabled={locked('diarizer')} class={inputClass}>
							<option value="auto">Automatic (Nemotron on an NVIDIA GPU, else pyannote)</option>
							<option value="nemotron">Nemotron (NVIDIA GPU, NeMo)</option>
							<option value="pyannote">pyannote (GPU, torch)</option>
							<option value="none">None (single speaker)</option>
						</select>
					</label>

					{#if form.transcriber === 'whisperx'}
						<label>
							<span class={labelClass}>Whisper model</span>
							<select bind:value={form.whisper_model_size} disabled={locked('whisper_model_size')} class={inputClass}>
								{#each WHISPER_MODELS as m}<option value={m}>{m}</option>{/each}
							</select>
						</label>
						<label>
							<span class={labelClass}>Compute type</span>
							<select bind:value={form.whisper_compute_type} disabled={locked('whisper_compute_type')} class={inputClass}>
								{#each COMPUTE_TYPES as c}<option value={c}>{c}</option>{/each}
							</select>
						</label>
						<label>
							<span class={labelClass}>Batch size</span>
							<input type="number" min="1" max="64" bind:value={form.whisper_batch_size} disabled={locked('whisper_batch_size')} class={inputClass} />
						</label>
					{:else if form.transcriber === 'parakeet'}
						<label>
							<span class={labelClass}>Parakeet model (onnx-asr name)</span>
							<input type="text" bind:value={form.parakeet_model} disabled={locked('parakeet_model')} class={inputClass} />
						</label>
						<label>
							<span class={labelClass}>Quantization</span>
							<select bind:value={form.parakeet_quantization} disabled={locked('parakeet_quantization')} class={inputClass}>
								<option value="">fp32 (larger; needs a non-symlinked model cache)</option>
								<option value="int8">int8 (default: smaller, faster on CPU)</option>
							</select>
						</label>
						<label>
							<span class={labelClass}>ONNX provider</span>
							<select bind:value={form.onnx_provider} disabled={locked('onnx_provider')} class={inputClass}>
								<option value="cpu">CPU</option>
								<option value="cuda">CUDA (needs onnxruntime-gpu)</option>
							</select>
						</label>
					{:else if form.transcriber === 'remote'}
						<label>
							<span class={labelClass}>Server base URL (…/v1)</span>
							<input type="text" bind:value={form.remote_stt_url} disabled={locked('remote_stt_url')} placeholder="http://127.0.0.1:8484/v1" class={inputClass} />
						</label>
						<label>
							<span class={labelClass}>Model name</span>
							<input type="text" bind:value={form.remote_stt_model} disabled={locked('remote_stt_model')} class={inputClass} />
						</label>
						<label>
							<span class={labelClass}>
								API key
								{#if settings.secrets_set.remote_stt_api_key}<span class="text-green-500 ml-1">set</span>{/if}
							</span>
							<div class="flex gap-2">
								<input type="password" bind:value={secrets.remote_stt_api_key} disabled={locked('remote_stt_api_key')} placeholder={settings.secrets_set.remote_stt_api_key ? '•••••••• (leave blank to keep)' : 'optional'} class={inputClass} />
								{#if settings.secrets_set.remote_stt_api_key && !locked('remote_stt_api_key')}
									<button onclick={() => clearSecret('remote_stt_api_key')} class="text-xs text-gray-500 hover:text-red-400">Clear</button>
								{/if}
							</div>
						</label>
					{/if}

					{#if form.diarizer !== 'none'}
						<label>
							<span class={labelClass}>pyannote model (also voice profiles)</span>
							<input type="text" bind:value={form.diarization_model} disabled={locked('diarization_model')} class={inputClass} />
						</label>
						<label>
							<span class={labelClass}>Max speakers</span>
							<input type="number" min="1" max="20" bind:value={form.max_speakers} disabled={locked('max_speakers')} class={inputClass} />
						</label>
						<label class="col-span-2">
							<span class={labelClass}>
								HuggingFace token (pyannote license; Nemotron needs it only for voice profiles)
								{#if settings.secrets_set.hf_token}<span class="text-green-500 ml-1">set</span>{/if}
							</span>
							<div class="flex gap-2">
								<input type="password" bind:value={secrets.hf_token} disabled={locked('hf_token')} placeholder={settings.secrets_set.hf_token ? '•••••••• (leave blank to keep)' : 'hf_...'} class={inputClass} />
								{#if settings.secrets_set.hf_token && !locked('hf_token')}
									<button onclick={() => clearSecret('hf_token')} class="text-xs text-gray-500 hover:text-red-400">Clear</button>
								{/if}
							</div>
						</label>
					{/if}

					<label>
						<span class={labelClass}>Language (blank = auto)</span>
						<input type="text" bind:value={form.language} disabled={locked('language')} placeholder="en" class={inputClass} />
					</label>
					<label>
						<span class={labelClass}>Your name in transcripts (mic channel)</span>
						<input type="text" bind:value={form.local_speaker_name} disabled={locked('local_speaker_name')} class={inputClass} />
					</label>
					<label class="flex items-center gap-2 col-span-2">
						<input type="checkbox" bind:checked={form.per_source_transcription} disabled={locked('per_source_transcription')} class="rounded border-gray-600 bg-gray-800" />
						<span class="text-sm text-gray-300">Transcribe mic and system audio separately (mic = you, no diarization needed)</span>
					</label>
					<label class="flex items-center gap-2 col-span-2">
						<input type="checkbox" bind:checked={form.auto_transcribe} disabled={locked('auto_transcribe')} class="rounded border-gray-600 bg-gray-800" />
						<span class="text-sm text-gray-300">Transcribe automatically after recording</span>
					</label>
				</div>
			</section>

			<!-- Glossary -->
			<section>
				<h3 class="text-lg font-semibold text-gray-200 mb-1">Names and terms</h3>
				<p class="text-xs text-gray-500 mb-3">
					One per line. Plain lines are names and jargon to spell correctly (used as a hint for WhisperX, for summaries and Ask).
					Lines like <code class="text-gray-400">walk you -> Waku</code> are corrections applied to every transcript (whole words, any case). <code class="text-gray-400">#</code> starts a comment.
				</p>
				<textarea
					bind:value={form.glossary}
					disabled={locked('glossary')}
					rows="6"
					placeholder={"Waku\nNimbus\nJakub Sokołowski\nwalk you -> Waku"}
					class="{inputClass} font-mono"
				></textarea>
				<label class="flex items-center gap-2 mt-2">
					<input type="checkbox" bind:checked={form.glossary_llm_correct} disabled={locked('glossary_llm_correct')} class="rounded border-gray-600 bg-gray-800" />
					<span class="text-sm text-gray-300">After transcription, let the LLM fix misheard names and terms from this list (uses the default provider; lines are never rephrased)</span>
				</label>
			</section>

			<div class="flex items-center gap-3">
				<button
					onclick={save}
					disabled={saving}
					class="px-4 py-1.5 text-sm rounded bg-blue-600 hover:bg-blue-700 disabled:bg-gray-700 text-white font-medium transition-colors"
				>
					{saving ? 'Saving...' : 'Save settings'}
				</button>
				<button onclick={load} disabled={loading} class="text-sm text-gray-400 hover:text-gray-200">Reload</button>
				{#if error}<span class="text-sm text-red-400">{error}</span>{/if}
			</div>
		{/if}

		{#if tab === 'ai'}
			<!-- LLM providers -->
			<section>
				<h3 class="text-lg font-semibold text-gray-200 mb-3">Summarization</h3>
				<div class="grid grid-cols-2 gap-3">
					<label>
						<span class={labelClass}>Ollama URL</span>
						<input type="text" bind:value={form.ollama_url} disabled={locked('ollama_url')} class={inputClass} />
					</label>
					<label>
						<span class={labelClass}>vLLM URL</span>
						<input type="text" bind:value={form.vllm_url} disabled={locked('vllm_url')} class={inputClass} />
					</label>
					<label>
						<span class={labelClass}>
							OpenAI API key
							{#if settings.secrets_set.openai_api_key}<span class="text-green-500 ml-1">set</span>{/if}
						</span>
						<div class="flex gap-2">
							<input type="password" bind:value={secrets.openai_api_key} disabled={locked('openai_api_key')} placeholder={settings.secrets_set.openai_api_key ? '•••••••• (leave blank to keep)' : 'sk-...'} class={inputClass} />
							{#if settings.secrets_set.openai_api_key && !locked('openai_api_key')}
								<button onclick={() => clearSecret('openai_api_key')} class="text-xs text-gray-500 hover:text-red-400">Clear</button>
							{/if}
						</div>
					</label>
					<label>
						<span class={labelClass}>
							Anthropic API key
							{#if settings.secrets_set.anthropic_api_key}<span class="text-green-500 ml-1">set</span>{/if}
						</span>
						<div class="flex gap-2">
							<input type="password" bind:value={secrets.anthropic_api_key} disabled={locked('anthropic_api_key')} placeholder={settings.secrets_set.anthropic_api_key ? '•••••••• (leave blank to keep)' : 'sk-ant-...'} class={inputClass} />
							{#if settings.secrets_set.anthropic_api_key && !locked('anthropic_api_key')}
								<button onclick={() => clearSecret('anthropic_api_key')} class="text-xs text-gray-500 hover:text-red-400">Clear</button>
							{/if}
						</div>
					</label>
					<label>
						<span class={labelClass}>Default provider</span>
						<select bind:value={form.default_provider} disabled={locked('default_provider')} class={inputClass}>
							{#each providers as p}<option value={p.provider}>{p.provider}</option>{/each}
							{#if !providers.some((p) => p.provider === form.default_provider)}
								<option value={form.default_provider}>{form.default_provider}</option>
							{/if}
						</select>
					</label>
					<label>
						<span class={labelClass}>Default model (blank = first available)</span>
						<input type="text" bind:value={form.default_model} disabled={locked('default_model')} list="model-options" class={inputClass} />
						<datalist id="model-options">
							{#each providers.find((p) => p.provider === form.default_provider)?.models ?? [] as m}
								<option value={m}></option>
							{/each}
						</datalist>
					</label>
					<label class="flex items-center gap-2 col-span-2">
						<input type="checkbox" bind:checked={form.cloud_redaction} disabled={locked('cloud_redaction')} class="rounded border-gray-600 bg-gray-800" />
						<span class="text-sm text-gray-300">Hide names, emails and phone numbers from OpenAI and Anthropic (swapped for placeholders, restored in the reply)</span>
					</label>
					<p class="col-span-2 text-[11px] text-gray-600 -mt-1">Meetings marked local-only (lock in the meeting header) are never sent to them at all.</p>
				</div>
			</section>

			<!-- Search -->
			<section>
				<h3 class="text-lg font-semibold text-gray-200 mb-1">Search and Ask</h3>
				<p class="text-xs text-gray-500 mb-3">
					Besides exact words, find passages by meaning ("spending" finds "costs"). Uses a small local model (downloaded once,
					about 250 MB, CPU only); meetings are indexed in the background.
				</p>
				<label class="flex items-center gap-2 mb-2">
					<input type="checkbox" bind:checked={form.semantic_search} disabled={locked('semantic_search')} class="rounded border-gray-600 bg-gray-800" />
					<span class="text-sm text-gray-300">Search by meaning too</span>
				</label>
				<label class="block max-w-md mb-2">
					<span class={labelClass}>Embedding model (Hugging Face, model2vec)</span>
					<input type="text" bind:value={form.embedding_model} disabled={locked('embedding_model') || !form.semantic_search} class={inputClass} />
				</label>
				{#if indexStatus}
					<p class="text-xs {indexStatus.error ? 'text-yellow-600' : 'text-gray-500'}">
						{#if indexStatus.error}
							{indexStatus.error}
						{:else if !indexStatus.enabled}
							Off.
						{:else}
							{indexStatus.indexed_sessions} of {indexStatus.total_sessions} meetings indexed{#if indexStatus.pending}, {indexStatus.pending} pending{/if}{#if !indexStatus.ready} · model not loaded yet{/if}.
						{/if}
						<button onclick={handleRebuild} class="ml-2 text-gray-400 hover:text-gray-200 underline">Rebuild</button>
					</p>
				{/if}
			</section>

			<!-- Provider status -->
			<section class="border-t border-gray-800 pt-4">
				<h3 class="text-sm font-semibold text-gray-300 mb-2">Provider status</h3>
				<div class="space-y-2">
					{#each providers as p}
						<div class="bg-gray-900 border border-gray-700 rounded-lg p-3">
							<div class="flex items-center justify-between mb-1">
								<span class="text-sm font-medium text-gray-200 capitalize">{p.provider}</span>
								<span class="text-xs px-2 py-0.5 rounded {p.models.length > 0 ? 'bg-green-900 text-green-300' : 'bg-gray-800 text-gray-500'}">
									{p.models.length} model{p.models.length !== 1 ? 's' : ''}
								</span>
							</div>
							{#if p.models.length > 0}
								<div class="flex flex-wrap gap-1">
									{#each p.models as model}
										<span class="text-xs bg-gray-800 text-gray-400 px-2 py-0.5 rounded">{model}</span>
									{/each}
								</div>
							{:else}
								<p class="text-xs text-gray-600">No models available (check connection or API key)</p>
							{/if}
						</div>
					{/each}
				</div>
			</section>

			<div class="flex items-center gap-3">
				<button
					onclick={save}
					disabled={saving}
					class="px-4 py-1.5 text-sm rounded bg-blue-600 hover:bg-blue-700 disabled:bg-gray-700 text-white font-medium transition-colors"
				>
					{saving ? 'Saving...' : 'Save settings'}
				</button>
				<button onclick={load} disabled={loading} class="text-sm text-gray-400 hover:text-gray-200">Reload</button>
				{#if error}<span class="text-sm text-red-400">{error}</span>{/if}
			</div>
		{/if}

		{#if tab === 'notes'}
			<!-- Summaries & export -->
			<section>
				<h3 class="text-lg font-semibold text-gray-200 mb-3">Summaries and export</h3>
				<div class="grid grid-cols-2 gap-3">
					<label>
						<span class={labelClass}>Default summary style</span>
						<select bind:value={form.summary_style} disabled={locked('summary_style')} class={inputClass}>
							<option value="meeting">meeting</option>
							<option value="standup">standup</option>
							<option value="interview">interview</option>
							<option value="lecture">lecture</option>
							<option value="brainstorm">brainstorm</option>
						</select>
					</label>
					<label>
						<span class={labelClass}>Obsidian tags (comma-separated)</span>
						<input type="text" bind:value={form.obsidian_tags} disabled={locked('obsidian_tags')} class={inputClass} />
					</label>
					<label>
						<span class={labelClass}>Summarize in parts above (characters; 0 = never)</span>
						<input type="number" min="0" step="5000" bind:value={form.summary_chunk_chars} disabled={locked('summary_chunk_chars')} class={inputClass} />
						<span class="block text-[11px] text-gray-600 mt-1">Long meetings are summarized part by part and merged. About 4 characters per token: 40000 fits an 16k-token model.</span>
					</label>
					<label class="col-span-2">
						<span class={labelClass}>Extra instructions for every summary (project names, jargon, what to emphasize)</span>
						<textarea bind:value={form.summary_instructions} disabled={locked('summary_instructions')} rows="3" class={inputClass}></textarea>
					</label>
					<label class="flex items-center gap-2 col-span-2">
						<input type="checkbox" bind:checked={form.auto_summarize} disabled={locked('auto_summarize')} class="rounded border-gray-600 bg-gray-800" />
						<span class="text-sm text-gray-300">Summarize automatically after transcription (uses the default provider, model and style)</span>
					</label>
					<label class="flex items-center gap-2 col-span-2">
						<input type="checkbox" bind:checked={form.auto_name_sessions} disabled={locked('auto_name_sessions')} class="rounded border-gray-600 bg-gray-800" />
						<span class="text-sm text-gray-300">Name untitled sessions from the summary</span>
					</label>
					<label class="flex items-center gap-2">
						<input type="checkbox" bind:checked={form.obsidian_link_people} disabled={locked('obsidian_link_people')} class="rounded border-gray-600 bg-gray-800" />
						<span class="text-sm text-gray-300">Link named participants as [[Person]]</span>
					</label>
					<label class="flex items-center gap-2">
						<input type="checkbox" bind:checked={form.obsidian_include_transcript} disabled={locked('obsidian_include_transcript')} class="rounded border-gray-600 bg-gray-800" />
						<span class="text-sm text-gray-300">Include the full transcript in exported notes</span>
					</label>
					<label class="flex items-center gap-2 col-span-2">
						<input type="checkbox" bind:checked={form.obsidian_people_notes} disabled={locked('obsidian_people_notes')} class="rounded border-gray-600 bg-gray-800" />
						<span class="text-sm text-gray-300">Keep a note per person (meetings and tasks) in the people/ folder, unless you already have a note for them</span>
					</label>
					<label class="flex items-center gap-2 col-span-2">
						<input type="checkbox" bind:checked={form.obsidian_auto_export} disabled={locked('obsidian_auto_export')} class="rounded border-gray-600 bg-gray-800" />
						<span class="text-sm text-gray-300">Export to Obsidian automatically after each summary (overwrites that session's note)</span>
					</label>
					<label>
						<span class={labelClass}>Weekly digest</span>
						<select bind:value={form.digest_weekday} disabled={locked('digest_weekday')} class={inputClass}>
							<option value={-1}>Off (generate from the Digest view)</option>
							{#each ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'] as day, i}
								<option value={i}>Every {day}</option>
							{/each}
						</select>
					</label>
					<label>
						<span class={labelClass}>At or after (hour, 0 to 23)</span>
						<input type="number" min="0" max="23" bind:value={form.digest_hour} disabled={locked('digest_hour') || (form.digest_weekday ?? -1) < 0} class={inputClass} />
					</label>
					<p class="col-span-2 text-[11px] text-gray-600 -mt-1">
						Covers Monday to Sunday of that week, once per week, while the app is running. Written to the vault's digests/ folder.
					</p>
				</div>
			</section>

			<!-- GitHub -->
			<section>
				<h3 class="text-lg font-semibold text-gray-200 mb-1">GitHub issues</h3>
				<p class="text-xs text-gray-500 mb-3">
					Turn selected action items into issues. Use a fine-grained token limited to this repository with "Issues: read and write". Save, then Test.
				</p>
				<div class="grid grid-cols-2 gap-3">
					<label>
						<span class={labelClass}>Repository (owner/name)</span>
						<input type="text" bind:value={form.github_repo} disabled={locked('github_repo')} placeholder="corpetty/notes" class={inputClass} />
					</label>
					<label>
						<span class={labelClass}>Labels (comma-separated)</span>
						<input type="text" bind:value={form.github_labels} disabled={locked('github_labels')} class={inputClass} />
					</label>
					<label class="col-span-2">
						<span class={labelClass}>
							Token
							{#if settings.secrets_set.github_token}<span class="text-green-500 ml-1">set</span>{/if}
						</span>
						<div class="flex gap-2">
							<input type="password" bind:value={secrets.github_token} disabled={locked('github_token')} placeholder={settings.secrets_set.github_token ? '•••••••• (leave blank to keep)' : 'github_pat_…'} class={inputClass} />
							{#if settings.secrets_set.github_token && !locked('github_token')}
								<button onclick={() => clearSecret('github_token')} class="text-xs text-gray-500 hover:text-red-400">Clear</button>
							{/if}
						</div>
					</label>
					<div class="flex items-center gap-2 col-span-2">
						<button onclick={testGitHub} class="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300">Test</button>
						{#if ghTest}<span class="text-xs text-gray-400">{ghTest}</span>{/if}
					</div>
				</div>
			</section>

			<!-- Other issue trackers and chat -->
			<section>
				<h3 class="text-lg font-semibold text-gray-200 mb-1">Linear, Jira, Slack and Matrix</h3>
				<p class="text-xs text-gray-500 mb-3">
					Action items can also become Linear or Jira issues, and follow-ups can be posted to Slack or a Matrix room. Fill in
					what you use, save, then Test.
				</p>
				{#snippet secret(key: SecretKey, placeholder: string, label: string)}
					<label class="block">
						<span class={labelClass}>{label}{#if settings?.secrets_set[key]}<span class="text-green-500 ml-1">set</span>{/if}</span>
						<div class="flex gap-2">
							<input type="password" bind:value={secrets[key]} disabled={locked(key)} placeholder={settings?.secrets_set[key] ? '•••••••• (leave blank to keep)' : placeholder} class={inputClass} />
							{#if settings?.secrets_set[key] && !locked(key)}
								<button onclick={() => clearSecret(key)} class="text-xs text-gray-500 hover:text-red-400">Clear</button>
							{/if}
						</div>
					</label>
				{/snippet}
				{#snippet test(name: 'linear' | 'jira' | 'slack' | 'matrix')}
					<div class="flex items-center gap-2 col-span-2">
						<button onclick={() => testIntegration(name)} class="px-3 py-1 text-xs rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300">Test</button>
						{#if integrationTest[name]}<span class="text-xs text-gray-400">{integrationTest[name]}</span>{/if}
					</div>
				{/snippet}
				<div class="space-y-4">
					<div class="grid grid-cols-2 gap-3">
						<h4 class="col-span-2 text-sm font-medium text-gray-300">Linear</h4>
						{@render secret('linear_api_key', 'lin_api_…', 'Personal API key')}
						<label>
							<span class={labelClass}>Team key</span>
							<input type="text" bind:value={form.linear_team} disabled={locked('linear_team')} placeholder="ENG" class={inputClass} />
						</label>
						{@render test('linear')}
					</div>
					<div class="grid grid-cols-2 gap-3">
						<h4 class="col-span-2 text-sm font-medium text-gray-300">Jira Cloud</h4>
						<label>
							<span class={labelClass}>Site</span>
							<input type="text" bind:value={form.jira_url} disabled={locked('jira_url')} placeholder="https://yourteam.atlassian.net" class={inputClass} />
						</label>
						<label>
							<span class={labelClass}>Email</span>
							<input type="text" bind:value={form.jira_email} disabled={locked('jira_email')} class={inputClass} />
						</label>
						{@render secret('jira_api_token', 'API token', 'API token')}
						<label>
							<span class={labelClass}>Project key · issue type</span>
							<div class="flex gap-2">
								<input type="text" bind:value={form.jira_project} disabled={locked('jira_project')} placeholder="OPS" class={inputClass} />
								<input type="text" bind:value={form.jira_issue_type} disabled={locked('jira_issue_type')} class={inputClass} />
							</div>
						</label>
						{@render test('jira')}
					</div>
					<div class="grid grid-cols-2 gap-3">
						<h4 class="col-span-2 text-sm font-medium text-gray-300">Slack</h4>
						<div class="col-span-2">{@render secret('slack_webhook_url', 'https://hooks.slack.com/services/…', 'Incoming webhook URL')}</div>
						{@render test('slack')}
					</div>
					<div class="grid grid-cols-2 gap-3">
						<h4 class="col-span-2 text-sm font-medium text-gray-300">Matrix</h4>
						<label>
							<span class={labelClass}>Homeserver</span>
							<input type="text" bind:value={form.matrix_homeserver} disabled={locked('matrix_homeserver')} placeholder="https://matrix.org" class={inputClass} />
						</label>
						<label>
							<span class={labelClass}>Room id</span>
							<input type="text" bind:value={form.matrix_room_id} disabled={locked('matrix_room_id')} placeholder="!abc123:matrix.org" class={inputClass} />
						</label>
						<div class="col-span-2">{@render secret('matrix_access_token', 'syt_…', 'Access token (of a bot or your account)')}</div>
						{@render test('matrix')}
					</div>
				</div>
			</section>

			<div class="flex items-center gap-3">
				<button
					onclick={save}
					disabled={saving}
					class="px-4 py-1.5 text-sm rounded bg-blue-600 hover:bg-blue-700 disabled:bg-gray-700 text-white font-medium transition-colors"
				>
					{saving ? 'Saving...' : 'Save settings'}
				</button>
				<button onclick={load} disabled={loading} class="text-sm text-gray-400 hover:text-gray-200">Reload</button>
				{#if error}<span class="text-sm text-red-400">{error}</span>{/if}
			</div>
		{/if}

		{#if tab === 'general'}
			<!-- Server mode -->
			<section>
				<h3 class="text-lg font-semibold text-gray-200 mb-1">Server mode</h3>
				<p class="text-xs text-gray-500 mb-3">
					To let other machines use this backend, run it with <code class="text-gray-400">--host 0.0.0.0</code> and set a token here; every request must then carry it.
				</p>
				<label class="block max-w-md">
					<span class={labelClass}>
						API token
						{#if settings.secrets_set.api_token}<span class="text-green-500 ml-1">set</span>{/if}
					</span>
					<div class="flex gap-2">
						<input type="password" bind:value={secrets.api_token} disabled={locked('api_token')} placeholder={settings.secrets_set.api_token ? '•••••••• (leave blank to keep)' : 'leave empty for local-only use'} class={inputClass} />
						{#if settings.secrets_set.api_token && !locked('api_token')}
							<button onclick={() => clearSecret('api_token')} class="text-xs text-gray-500 hover:text-red-400">Clear</button>
						{/if}
					</div>
				</label>
				<PhoneLink />
			</section>

			{#if updateState.supported}
				<!-- Updates -->
				<section>
					<h3 class="text-lg font-semibold text-gray-200 mb-1">Updates</h3>
					<p class="text-xs text-gray-500 mb-3">
						Checked once at start. Updates are signed and downloaded from the GitHub release; deb and rpm installs ask for your password.
					</p>
					<div class="flex items-center gap-3 text-sm">
						<button
							onclick={() => updateState.check()}
							disabled={updateState.status === 'checking' || updateState.status === 'downloading'}
							class="px-3 py-1.5 text-xs rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200 disabled:opacity-50"
						>
							{updateState.status === 'checking' ? 'Checking…' : 'Check for updates'}
						</button>
						{#if updateState.status === 'none'}
							<span class="text-gray-400">Up to date ({updateState.current})</span>
						{:else if updateState.status === 'available'}
							<span class="text-emerald-400">{updateState.version} is available</span>
							<button onclick={() => updateState.install()} class="text-xs text-emerald-400 hover:text-emerald-300 underline">Update and restart</button>
						{:else if updateState.status === 'error'}
							<span class="text-red-400 text-xs">{updateState.error}</span>
						{/if}
					</div>
				</section>
			{/if}

			<StorageSettings locked={locked('audio_retention_days')} />

			<div class="flex items-center gap-3">
				<button
					onclick={save}
					disabled={saving}
					class="px-4 py-1.5 text-sm rounded bg-blue-600 hover:bg-blue-700 disabled:bg-gray-700 text-white font-medium transition-colors"
				>
					{saving ? 'Saving...' : 'Save settings'}
				</button>
				<button onclick={load} disabled={loading} class="text-sm text-gray-400 hover:text-gray-200">Reload</button>
				{#if error}<span class="text-sm text-red-400">{error}</span>{/if}
			</div>
		{/if}

		<p class="text-xs text-gray-600">
			Saved to <code class="text-gray-500">{settings.config_file}</code>.
			{#if settings.env_overrides.length > 0}
				Locked fields are set by environment variables: {settings.env_overrides.join(', ')}.
			{/if}
		</p>
	{:else if error}
		<p class="text-sm text-red-400">{error}</p>
	{/if}
</div>
