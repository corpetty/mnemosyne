<script lang="ts">
	import { getSettings, getSystemInfo, listModels, updateSettings } from '$lib/api/backend.js';
	import { startRecording } from '$lib/app/controller.svelte.js';
	import { audioState } from '$lib/stores/audio.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { uiState } from '$lib/stores/ui.svelte.js';
	import type { ProviderModels, SettingsUpdate, SystemInfo } from '$lib/types/index.js';
	import DeviceSelector from './DeviceSelector.svelte';

	const STEPS = ['Welcome', 'Audio', 'Transcription', 'Summaries', 'Notes & calendar', 'Done'] as const;
	let step = $state(0);
	let saving = $state(false);
	let system = $state<SystemInfo | null>(null);

	// Transcription
	let transcriber = $state<'parakeet' | 'whisperx' | 'remote'>('parakeet');
	let remoteUrl = $state('');
	let identifySpeakers = $state(false);
	let hfToken = $state('');
	// Summaries
	let provider = $state<'ollama' | 'vllm' | 'openai' | 'anthropic' | 'none'>('ollama');
	let ollamaUrl = $state('http://localhost:11434');
	let vllmUrl = $state('http://localhost:8000');
	let apiKey = $state('');
	let models = $state<ProviderModels[] | null>(null);
	let model = $state('');
	let checking = $state(false);
	// Notes
	let vaultPath = $state('');
	let calendarUrl = $state('');

	$effect(() => {
		// Start from the current configuration; the system probe only adds hints.
		getSystemInfo()
			.then((s) => (system = s))
			.catch(() => {});
		getSettings()
			.then((st) => {
				const v = st.values;
				if (v.transcriber === 'parakeet' || v.transcriber === 'whisperx' || v.transcriber === 'remote') {
					transcriber = v.transcriber;
				}
				remoteUrl = v.remote_stt_url || '';
				identifySpeakers = v.diarizer !== 'none';
				ollamaUrl = v.ollama_url || ollamaUrl;
				vllmUrl = v.vllm_url || vllmUrl;
				vaultPath = v.obsidian_vault_path || '';
				if (['ollama', 'vllm', 'openai', 'anthropic'].includes(v.default_provider)) {
					provider = v.default_provider as typeof provider;
				}
				model = v.default_model || '';
			})
			.catch(() => {});
	});

	async function save(update: SettingsUpdate) {
		saving = true;
		try {
			await updateSettings(update);
			return true;
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not save');
			return false;
		} finally {
			saving = false;
		}
	}

	function transcriptionUpdate(): SettingsUpdate {
		const u: SettingsUpdate = { transcriber, diarizer: identifySpeakers ? 'auto' : 'none' };
		if (transcriber === 'remote') u.remote_stt_url = remoteUrl.trim();
		if (hfToken.trim()) u.hf_token = hfToken.trim();
		return u;
	}

	function providerUpdate(): SettingsUpdate {
		if (provider === 'none') return {};
		const u: SettingsUpdate = { default_provider: provider, default_model: model };
		if (provider === 'ollama') u.ollama_url = ollamaUrl.trim();
		if (provider === 'vllm') u.vllm_url = vllmUrl.trim();
		if (provider === 'openai' && apiKey.trim()) u.openai_api_key = apiKey.trim();
		if (provider === 'anthropic' && apiKey.trim()) u.anthropic_api_key = apiKey.trim();
		return u;
	}

	async function checkProvider() {
		checking = true;
		models = null;
		if (await save({ ...providerUpdate(), default_model: '' })) {
			try {
				models = await listModels();
				const found = models.find((p) => p.provider === provider)?.models ?? [];
				if (found.length && !found.includes(model)) model = found[0];
			} catch {
				models = [];
			}
		}
		checking = false;
	}

	const found = $derived(models?.find((p) => p.provider === provider)?.models ?? []);

	async function next() {
		let ok = true;
		if (STEPS[step] === 'Transcription') ok = await save(transcriptionUpdate());
		if (STEPS[step] === 'Summaries') ok = await save(providerUpdate());
		if (STEPS[step] === 'Notes & calendar') {
			const u: SettingsUpdate = { obsidian_vault_path: vaultPath.trim() };
			if (calendarUrl.trim()) u.calendar_ics_url = calendarUrl.trim();
			ok = await save(u);
		}
		if (ok) step = Math.min(step + 1, STEPS.length - 1);
	}

	async function finish(record = false) {
		await save({ setup_complete: true });
		uiState.view = 'home';
		if (record) startRecording(true);
	}

	async function browseVault() {
		try {
			const { open } = await import('@tauri-apps/plugin-dialog');
			const picked = await open({ directory: true, title: 'Your Obsidian vault' });
			if (typeof picked === 'string') vaultPath = picked;
		} catch {
			/* not in the desktop app: type the path */
		}
	}

	const input = 'w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200';
	const option = (on: boolean) =>
		`flex gap-3 items-start rounded-lg border p-3 cursor-pointer ${on ? 'border-blue-600 bg-blue-950/30' : 'border-gray-800 hover:border-gray-700'}`;
</script>

<div class="space-y-6" aria-label="Setup">
	<ol class="flex flex-wrap gap-2 text-xs" aria-label="Setup steps">
		{#each STEPS as s, i}
			<li class="px-2 py-0.5 rounded-full {i === step ? 'bg-blue-600 text-white' : i < step ? 'bg-gray-800 text-gray-300' : 'text-gray-600'}">
				{i + 1}. {s}
			</li>
		{/each}
	</ol>

	{#if STEPS[step] === 'Welcome'}
		<section class="space-y-3">
			<h2 class="text-2xl font-semibold text-gray-100">Welcome to Mnemosyne</h2>
			<p class="text-gray-300">
				Mnemosyne records your meetings (your microphone and what your computer plays), transcribes them on this machine,
				and writes summaries with the language model of your choice.
			</p>
			<p class="text-sm text-gray-500">A few steps: pick your audio, choose how to transcribe, connect a model for summaries, and optionally your notes and calendar. Everything can be changed later in Settings.</p>
			{#if system && (!system.pipewire || !system.ffmpeg)}
				<p class="text-sm text-yellow-500">
					Missing on this machine: {[!system.pipewire && 'PipeWire tools (pw-record)', !system.ffmpeg && 'ffmpeg'].filter(Boolean).join(' and ')}. Recording needs them.
				</p>
			{/if}
		</section>
	{:else if STEPS[step] === 'Audio'}
		<section class="space-y-3">
			<h2 class="text-xl font-semibold text-gray-100">What should be recorded?</h2>
			<p class="text-sm text-gray-400">
				Tick your <strong>microphone</strong> and the <strong>output your meetings play through</strong> (speakers or
				headphones). Use <em>check</em> to see your mic level and <em>test capture</em> to confirm system audio is really
				recorded.
			</p>
			<DeviceSelector />
			{#if audioState.selectedDeviceIds.size === 0}
				<p class="text-xs text-gray-500">Nothing selected yet; you can also choose later on a meeting's Recording tab.</p>
			{/if}
		</section>
	{:else if STEPS[step] === 'Transcription'}
		<section class="space-y-3">
			<h2 class="text-xl font-semibold text-gray-100">How should meetings be transcribed?</h2>
			<label class={option(transcriber === 'parakeet')}>
				<input type="radio" bind:group={transcriber} value="parakeet" class="mt-1" />
				<span><span class="text-gray-100 font-medium">Parakeet</span> <span class="text-xs text-gray-500">recommended without a GPU</span><br /><span class="text-sm text-gray-400">Fast and accurate on the CPU, English and European languages.</span></span>
			</label>
			<label class={option(transcriber === 'whisperx')}>
				<input type="radio" bind:group={transcriber} value="whisperx" class="mt-1" disabled={system !== null && !system.gpu_stack && !system.gpu_driver} />
				<span><span class="text-gray-100 font-medium">WhisperX</span> <span class="text-xs text-gray-500">NVIDIA GPU</span><br />
					<span class="text-sm text-gray-400">
						Whisper large on the GPU, many languages.
						{#if system && !system.gpu_driver}No NVIDIA driver found on this machine.{:else if system && !system.gpu_stack}GPU support is still installing; Parakeet works meanwhile.{/if}
					</span></span>
			</label>
			<label class={option(transcriber === 'remote')}>
				<input type="radio" bind:group={transcriber} value="remote" class="mt-1" />
				<span><span class="text-gray-100 font-medium">A transcription server</span><br /><span class="text-sm text-gray-400">Any OpenAI-compatible speech-to-text endpoint.</span></span>
			</label>
			{#if transcriber === 'remote'}
				<input bind:value={remoteUrl} placeholder="http://my-server:8484/v1" class={input} aria-label="Transcription server URL" />
			{/if}
			<label class="flex items-start gap-2 pt-2">
				<input type="checkbox" bind:checked={identifySpeakers} class="mt-1 rounded border-gray-600 bg-gray-800" />
				<span class="text-sm text-gray-300">
					Tell speakers apart
					<span class="block text-xs text-gray-500">On an NVIDIA GPU this uses Nemotron and needs no account. Otherwise it uses pyannote, which needs a free Hugging Face token with speaker-diarization-community-1 accepted; the token also lets Mnemosyne recognise voices across meetings. Without either, lines are labelled by channel (you vs. everyone else).</span>
				</span>
			</label>
			{#if identifySpeakers && !system?.hf_token}
				<input type="password" bind:value={hfToken} placeholder="hf_…" class={input} aria-label="Hugging Face token" />
			{/if}
		</section>
	{:else if STEPS[step] === 'Summaries'}
		<section class="space-y-3">
			<h2 class="text-xl font-semibold text-gray-100">Which model writes summaries?</h2>
			<p class="text-sm text-gray-400">Local servers keep everything on your network. Cloud models are optional; meetings you mark local-only never go to them.</p>
			<div class="grid gap-2 sm:grid-cols-2">
				{#each [['ollama', 'Ollama', 'local'], ['vllm', 'vLLM', 'local or LAN'], ['openai', 'OpenAI', 'cloud'], ['anthropic', 'Anthropic', 'cloud'], ['none', 'Not now', 'summaries off']] as [id, label, hint]}
					<label class={option(provider === id)}>
						<input type="radio" bind:group={provider} value={id} class="mt-1" onchange={() => (models = null)} />
						<span><span class="text-gray-100 font-medium">{label}</span> <span class="text-xs text-gray-500">{hint}</span></span>
					</label>
				{/each}
			</div>
			{#if provider === 'ollama'}
				<input bind:value={ollamaUrl} class={input} aria-label="Ollama URL" />
			{:else if provider === 'vllm'}
				<input bind:value={vllmUrl} class={input} aria-label="vLLM URL" />
			{:else if provider === 'openai' || provider === 'anthropic'}
				<input type="password" bind:value={apiKey} placeholder={provider === 'openai' ? 'sk-…' : 'sk-ant-…'} class={input} aria-label="API key" />
			{/if}
			{#if provider !== 'none'}
				<div class="flex items-center gap-3">
					<button onclick={checkProvider} disabled={checking || saving} class="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200 disabled:opacity-50">
						{checking ? 'Checking…' : 'Check connection'}
					</button>
					{#if models !== null}
						{#if found.length}
							<span class="text-sm text-green-400">✓ {found.length} model{found.length === 1 ? '' : 's'}</span>
							<select bind:value={model} class="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-200" aria-label="Model">
								{#each found as m}<option value={m}>{m}</option>{/each}
							</select>
						{:else}
							<span class="text-sm text-red-400">No models found. Is the server running at that address?</span>
						{/if}
					{/if}
				</div>
			{/if}
		</section>
	{:else if STEPS[step] === 'Notes & calendar'}
		<section class="space-y-4">
			<h2 class="text-xl font-semibold text-gray-100">Notes and calendar <span class="text-sm font-normal text-gray-500">(optional)</span></h2>
			<label class="block space-y-1">
				<span class="text-sm text-gray-300">Obsidian vault</span>
				<span class="block text-xs text-gray-500">Meetings can be exported as notes with [[people]] links and tasks.</span>
				<span class="flex gap-2">
					<input bind:value={vaultPath} placeholder="/home/you/Notes" class={input} aria-label="Obsidian vault path" />
					<button onclick={browseVault} class="px-3 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300">Browse</button>
				</span>
			</label>
			<label class="block space-y-1">
				<span class="text-sm text-gray-300">Calendar (private ICS address)</span>
				<span class="block text-xs text-gray-500">Recordings are named after the meeting in progress, and you get a brief of what's open from last time.</span>
				<input type="password" bind:value={calendarUrl} placeholder="https://calendar.google.com/…/basic.ics" class={input} aria-label="Calendar ICS address" />
			</label>
		</section>
	{:else}
		<section class="space-y-3">
			<h2 class="text-2xl font-semibold text-gray-100">You're set</h2>
			<ul class="text-sm text-gray-300 space-y-1">
				<li>Audio: {audioState.selectedDeviceIds.size ? `${audioState.selectedDeviceIds.size} source${audioState.selectedDeviceIds.size === 1 ? '' : 's'} selected` : 'choose on the Recording tab'}</li>
				<li>Transcription: {transcriber === 'parakeet' ? 'Parakeet' : transcriber === 'whisperx' ? 'WhisperX' : 'transcription server'}{identifySpeakers ? ', with speaker identification' : ''}</li>
				<li>Summaries: {provider === 'none' ? 'off for now' : `${provider}${model ? ` · ${model}` : ''}`}</li>
				<li>Obsidian: {vaultPath || 'not connected'}</li>
			</ul>
			<p class="text-sm text-gray-500">Tip: Settings → Recording can start recordings automatically when a meeting app uses your microphone.</p>
			<div class="flex gap-3 pt-2">
				<button onclick={() => finish(true)} disabled={audioState.selectedDeviceIds.size === 0} class="px-4 py-2 rounded bg-red-600 hover:bg-red-700 disabled:bg-gray-700 disabled:text-gray-500 text-white font-medium">Start a recording</button>
				<button onclick={() => finish(false)} class="px-4 py-2 rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200">Go to the app</button>
			</div>
		</section>
	{/if}

	{#if STEPS[step] !== 'Done'}
		<div class="flex items-center gap-3 border-t border-gray-800 pt-4">
			{#if step > 0}
				<button onclick={() => (step -= 1)} class="px-3 py-1.5 text-sm rounded text-gray-400 hover:text-gray-200">Back</button>
			{/if}
			<button onclick={next} disabled={saving} class="px-4 py-1.5 text-sm rounded bg-blue-600 hover:bg-blue-700 disabled:bg-gray-700 text-white font-medium">
				{step === 0 ? 'Get started' : 'Next'}
			</button>
			<button onclick={() => finish(false)} class="ml-auto text-xs text-gray-500 hover:text-gray-300">Skip setup</button>
		</div>
	{/if}
</div>
