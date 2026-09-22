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
	let secrets = $state<{ hf_token: string; openai_api_key: string; anthropic_api_key: string }>({
		hf_token: '',
		openai_api_key: '',
		anthropic_api_key: ''
	});

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
				whisper_model_size: v.whisper_model_size,
				whisper_compute_type: v.whisper_compute_type,
				whisper_batch_size: v.whisper_batch_size,
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
			for (const key of ['hf_token', 'openai_api_key', 'anthropic_api_key'] as const) {
				if (secrets[key] !== '') update[key] = secrets[key];
			}
			settings = await updateSettings(update);
			secrets = { hf_token: '', openai_api_key: '', anthropic_api_key: '' };
			providers = await listModels();
			toastState.success('Settings saved');
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to save settings';
		} finally {
			saving = false;
		}
	}

	async function clearSecret(key: 'hf_token' | 'openai_api_key' | 'anthropic_api_key') {
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
				<label class="flex items-end gap-2 pb-1.5">
					<input type="checkbox" bind:checked={form.auto_transcribe} disabled={locked('auto_transcribe')} class="rounded border-gray-600 bg-gray-800" />
					<span class="text-sm text-gray-300">Transcribe automatically after recording</span>
				</label>
				<label class="col-span-2">
					<span class={labelClass}>
						HuggingFace token (diarization)
						{#if settings.secrets_set.hf_token}<span class="text-green-500 ml-1">set</span>{/if}
					</span>
					<div class="flex gap-2">
						<input type="password" bind:value={secrets.hf_token} disabled={locked('hf_token')} placeholder={settings.secrets_set.hf_token ? '•••••••• (leave blank to keep)' : 'hf_...'} class={inputClass} />
						{#if settings.secrets_set.hf_token && !locked('hf_token')}
							<button onclick={() => clearSecret('hf_token')} class="text-xs text-gray-500 hover:text-red-400">Clear</button>
						{/if}
					</div>
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
