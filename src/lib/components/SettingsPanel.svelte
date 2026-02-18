<script lang="ts">
	import { listModels } from '$lib/api/backend.js';
	import type { ProviderModels } from '$lib/types/index.js';

	let providers = $state<ProviderModels[]>([]);
	let loading = $state(false);

	async function refreshModels() {
		loading = true;
		try {
			providers = await listModels();
		} catch (e) {
			console.error('Failed to load models:', e);
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		refreshModels();
	});
</script>

<div class="space-y-6">
	<div>
		<h3 class="text-lg font-semibold text-gray-200 mb-4">LLM Providers</h3>
		<p class="text-sm text-gray-400 mb-4">
			Configure provider URLs and API keys in the backend <code class="text-gray-300">.env</code> file.
		</p>

		<div class="space-y-3">
			{#each providers as p}
				<div class="bg-gray-900 border border-gray-700 rounded-lg p-3">
					<div class="flex items-center justify-between mb-2">
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

			{#if providers.length === 0 && !loading}
				<p class="text-gray-500 text-sm">No providers configured.</p>
			{/if}
		</div>

		<button
			onclick={refreshModels}
			disabled={loading}
			class="mt-3 px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 transition-colors"
		>
			{loading ? 'Refreshing...' : 'Refresh Models'}
		</button>
	</div>

	<div class="border-t border-gray-800 pt-4">
		<h3 class="text-lg font-semibold text-gray-200 mb-2">Configuration</h3>
		<p class="text-sm text-gray-400">
			Edit <code class="text-gray-300">backend/.env</code> to configure:
		</p>
		<ul class="text-sm text-gray-400 mt-2 space-y-1 list-disc list-inside">
			<li><code class="text-gray-300">OLLAMA_URL</code> — Ollama server address</li>
			<li><code class="text-gray-300">VLLM_URL</code> — vLLM server address</li>
			<li><code class="text-gray-300">OPENAI_API_KEY</code> — OpenAI API key</li>
			<li><code class="text-gray-300">ANTHROPIC_API_KEY</code> — Anthropic API key</li>
			<li><code class="text-gray-300">HF_TOKEN</code> — HuggingFace token (for diarization)</li>
		</ul>
	</div>
</div>
