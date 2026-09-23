<script lang="ts">
	import { deleteSpeakerProfile, getSettings, listModels, listSpeakers, renameSpeakerProfile, updateSettings } from '$lib/api/backend.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { connectionState, LOCAL_BACKEND } from '$lib/stores/connection.svelte.js';
	import StorageSettings from './StorageSettings.svelte';
	import type { ProviderModels, SettingsResponse, SettingsUpdate, SpeakerProfile } from '$lib/types/index.js';

	let settings = $state<SettingsResponse | null>(null);
	let providers = $state<ProviderModels[]>([]);
	let voices = $state<SpeakerProfile[]>([]);
	let loading = $state(false);
	let saving = $state(false);
	let error = $state('');

	// Editable copy of the values
	let form = $state<SettingsUpdate>({});
	// Secret inputs are separate: '' = keep current, text = replace
	type SecretKey = 'hf_token' | 'openai_api_key' | 'anthropic_api_key' | 'remote_stt_api_key' | 'api_token';
	const SECRET_KEYS: SecretKey[] = ['hf_token', 'openai_api_key', 'anthropic_api_key', 'remote_stt_api_key', 'api_token'];
	const emptySecrets = (): Record<SecretKey, string> => ({
		hf_token: '',
		openai_api_key: '',
		anthropic_api_key: '',
		remote_stt_api_key: '',
		api_token: ''
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
				auto_summarize: v.auto_summarize,
				auto_name_sessions: v.auto_name_sessions,
				ollama_url: v.ollama_url,
				vllm_url: v.vllm_url,
				default_provider: v.default_provider,
				default_model: v.default_model,
				summary_style: v.summary_style,
				summary_instructions: v.summary_instructions,
				obsidian_tags: v.obsidian_tags,
				obsidian_link_people: v.obsidian_link_people,
				obsidian_include_transcript: v.obsidian_include_transcript
			};
			providers = await listModels();
			voices = await listSpeakers();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load settings';
		} finally {
			loading = false;
		}
	}

	async function save() {
		saving = true;
		error = '';
		try {
			const update: SettingsUpdate = { ...form };
			for (const key of SECRET_KEYS) {
				if (secrets[key] !== '') update[key] = secrets[key];
			}
			settings = await updateSettings(update);
			secrets = emptySecrets();
			providers = await listModels();
			toastState.success('Settings saved');
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to save settings';
		} finally {
			saving = false;
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

	const inputClass =
		'bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 disabled:opacity-60 w-full';
	const labelClass = 'block text-xs text-gray-500 mb-1';
</script>

<div class="space-y-6">
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

	{#if loading && !settings}
		<p class="text-gray-500 text-sm">Loading settings...</p>
	{:else if settings}
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

				{#if form.diarizer === 'pyannote'}
					<label>
						<span class={labelClass}>Diarization model</span>
						<input type="text" bind:value={form.diarization_model} disabled={locked('diarization_model')} class={inputClass} />
					</label>
					<label>
						<span class={labelClass}>Max speakers</span>
						<input type="number" min="1" max="20" bind:value={form.max_speakers} disabled={locked('max_speakers')} class={inputClass} />
					</label>
					<label class="col-span-2">
						<span class={labelClass}>
							HuggingFace token (pyannote license)
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
				<label>
					<span class={labelClass}>Label for the system-audio channel</span>
					<input type="text" bind:value={form.remote_speaker_name} disabled={locked('remote_speaker_name')} class={inputClass} />
				</label>
			</div>
		</section>

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

		<StorageSettings locked={locked('audio_retention_days')} />

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
