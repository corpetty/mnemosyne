<script lang="ts">
	import { listModels, summarizeSession } from '$lib/api/backend.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import type { ProviderModels } from '$lib/types/index.js';

	let providers = $state<ProviderModels[]>([]);
	let selectedProvider = $state('ollama');
	let selectedModel = $state('');
	let loading = $state(false);
	let error = $state('');
	let modelsLoaded = $state(false);

	async function loadModels() {
		try {
			providers = await listModels();
			modelsLoaded = true;
			// Auto-select first model of selected provider
			const p = providers.find((p) => p.provider === selectedProvider);
			if (p && p.models.length > 0 && !selectedModel) {
				selectedModel = p.models[0];
			}
		} catch (e) {
			console.error('Failed to load models:', e);
		}
	}

	function availableModels(): string[] {
		return providers.find((p) => p.provider === selectedProvider)?.models ?? [];
	}

	function handleProviderChange() {
		const models = availableModels();
		selectedModel = models.length > 0 ? models[0] : '';
	}

	async function handleSummarize() {
		const session = sessionState.activeSession;
		if (!session) return;

		loading = true;
		error = '';
		try {
			await summarizeSession(session.id, selectedProvider, selectedModel);
			await sessionState.refreshActive();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Summarization failed';
		} finally {
			loading = false;
		}
	}

	async function copyToClipboard() {
		const summary = sessionState.activeSession?.summary;
		if (summary) {
			await navigator.clipboard.writeText(summary);
		}
	}

	$effect(() => {
		if (!modelsLoaded) {
			loadModels();
		}
	});
</script>

<div class="space-y-3">
	{#if sessionState.activeSession?.summary}
		<div class="relative">
			<div class="bg-gray-900 border border-gray-700 rounded-lg p-4 prose prose-invert prose-sm max-w-none">
				{@html ''}
				<pre class="whitespace-pre-wrap text-sm text-gray-200 font-sans">{sessionState.activeSession.summary}</pre>
			</div>
			<button
				onclick={copyToClipboard}
				class="absolute top-2 right-2 text-gray-500 hover:text-gray-300 text-xs px-2 py-1 rounded bg-gray-800 border border-gray-700 transition-colors"
				title="Copy to clipboard"
			>
				Copy
			</button>
		</div>
	{/if}

	<!-- Summarize controls -->
	<div class="flex items-center gap-3">
		<select
			bind:value={selectedProvider}
			onchange={handleProviderChange}
			class="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200"
		>
			{#each providers as p}
				<option value={p.provider}>{p.provider}</option>
			{/each}
		</select>

		<select
			bind:value={selectedModel}
			class="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 min-w-[200px]"
		>
			{#each availableModels() as model}
				<option value={model}>{model}</option>
			{/each}
			{#if availableModels().length === 0}
				<option value="">No models available</option>
			{/if}
		</select>

		<button
			onclick={handleSummarize}
			disabled={loading || !sessionState.activeSession?.transcript?.length}
			class="px-4 py-1.5 text-sm rounded bg-purple-600 hover:bg-purple-700 disabled:bg-gray-700 disabled:text-gray-500 text-white font-medium transition-colors"
		>
			{loading ? 'Summarizing...' : sessionState.activeSession?.summary ? 'Re-summarize' : 'Summarize'}
		</button>
	</div>

	{#if error}
		<p class="text-red-400 text-sm">{error}</p>
	{/if}

	{#if !sessionState.activeSession?.transcript?.length}
		<p class="text-gray-600 text-sm">Record and transcribe audio first to generate a summary.</p>
	{/if}
</div>
