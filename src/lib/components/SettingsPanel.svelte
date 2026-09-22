<script lang="ts">
	import { getSettings, listModels, updateSettings } from '$lib/api/backend.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { ProviderModels, SettingsResponse, SettingsUpdate } from '$lib/types/index.js';

	let settings = $state<SettingsResponse | null>(null);
	let providers = $state<ProviderModels[]>([]);
	let loading = $state(false);
	let saving = $state(false);
	let error = $state('');

	// Editable copy of the values
	let form = $state<SettingsUpdate>({});
	// Secret inputs are separate: '' = keep current, text = replace
	type SecretKey = 'hf_token' | 'openai_api_key' | 'anthropic_api_key' | 'remote_stt_api_key';
	const SECRET_KEYS: SecretKey[] = ['hf_token', 'openai_api_key', 'anthropic_api_key', 'remote_stt_api_key'];
	const emptySecrets = (): Record<SecretKey, string> => ({
		hf_token: '',
		openai_api_key: '',
		anthropic_api_key: '',
		remote_stt_api_key: ''
	});
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
				per_source_transcription: v.per_source_transcription,
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
				ollama_url: v.ollama_url,
				vllm_url: v.vllm_url,
				default_provider: v.default_provider,
				default_model: v.default_model
			};
			providers = await listModels();
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

	const inputClass =
		'bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 disabled:opacity-60 w-full';
	const labelClass = 'block text-xs text-gray-500 mb-1';
</script>

<div class="space-y-6">
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
							<option value="">fp32 (best)</option>
							<option value="int8">int8 (smaller, faster on CPU)</option>
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
