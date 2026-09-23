<script lang="ts">
	import { listModels, listSummaryStyles, summarizeSession, getSettings } from '$lib/api/backend.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { jobsState } from '$lib/stores/jobs.svelte.js';
	import type { ProviderModels, SummaryStyle } from '$lib/types/index.js';

	let providers = $state<ProviderModels[]>([]);
	let styles = $state<SummaryStyle[]>([]);
	let selectedProvider = $state('');
	let selectedModel = $state('');
	let selectedStyle = $state('');
	let error = $state('');
	let loaded = $state(false);

	async function load() {
		try {
			const [p, s, settings] = await Promise.all([listModels(), listSummaryStyles(), getSettings()]);
			providers = p;
			styles = s;
			selectedProvider = settings.values.default_provider || p[0]?.provider || '';
			selectedModel = settings.values.default_model || '';
			selectedStyle = settings.values.summary_style || 'meeting';
			if (!selectedModel) {
				const prov = providers.find((x) => x.provider === selectedProvider);
				if (prov?.models.length) selectedModel = prov.models[0];
			}
			loaded = true;
		} catch (e) {
			console.error('Failed to load summary options:', e);
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
		error = '';
		try {
			const job = await summarizeSession(session.id, selectedProvider, selectedModel, selectedStyle);
			jobsState.track(job);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Summarization failed';
		}
	}

	async function copyToClipboard() {
		const s = sessionState.activeSession;
		if (!s?.summary) return;
		const d = s.summary_data;
		let text = s.summary;
		if (d) {
			if (d.decisions.length) text += '\n\n## Decisions\n' + d.decisions.map((x) => `- ${x}`).join('\n');
			if (d.action_items.length)
				text += '\n\n## Action Items\n' + d.action_items.map((a) => `- [ ] ${a.text}${a.owner ? ` (${a.owner})` : ''}`).join('\n');
			if (d.open_questions.length) text += '\n\n## Open Questions\n' + d.open_questions.map((x) => `- ${x}`).join('\n');
		}
		await navigator.clipboard.writeText(text);
		toastState.info('Summary copied');
	}

	$effect(() => {
		if (!loaded) load();
	});

	const data = $derived(sessionState.activeSession?.summary_data ?? null);
	const activeJob = $derived(
		sessionState.activeSession ? jobsState.active(sessionState.activeSession.id, 'summarize') : null
	);
	const lastJob = $derived(
		sessionState.activeSession ? jobsState.last(sessionState.activeSession.id, 'summarize') : null
	);
	const jobError = $derived(lastJob?.status === 'failed' ? lastJob.error : null);
</script>

<div class="space-y-3">
	{#if sessionState.activeSession?.summary}
		<div class="relative space-y-3">
			{#if data && data.topics.length}
				<div class="flex flex-wrap gap-1">
					{#each data.topics as t}
						<span class="text-[11px] px-2 py-0.5 rounded-full bg-gray-800 text-gray-400 border border-gray-700">{t}</span>
					{/each}
				</div>
			{/if}
			<div class="bg-gray-900 border border-gray-700 rounded-lg p-4">
				<pre class="whitespace-pre-wrap text-sm text-gray-200 font-sans">{sessionState.activeSession.summary}</pre>
			</div>
			{#if data}
				<div class="grid gap-3 md:grid-cols-2">
					{#if data.decisions.length}
						<section class="bg-gray-900 border border-gray-700 rounded-lg p-3">
							<h4 class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-2">Decisions</h4>
							<ul class="space-y-1 text-sm text-gray-200 list-disc list-inside">
								{#each data.decisions as d}<li>{d}</li>{/each}
							</ul>
						</section>
					{/if}
					{#if data.action_items.length}
						<section class="bg-gray-900 border border-gray-700 rounded-lg p-3">
							<h4 class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-2">Action items</h4>
							<ul class="space-y-1 text-sm text-gray-200">
								{#each data.action_items as a}
									<li class="flex gap-2"><span class="text-gray-600">☐</span><span>{a.text}{#if a.owner}<span class="text-gray-500"> · {a.owner}</span>{/if}</span></li>
								{/each}
							</ul>
						</section>
					{/if}
					{#if data.open_questions.length}
						<section class="bg-gray-900 border border-gray-700 rounded-lg p-3 md:col-span-2">
							<h4 class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-2">Open questions</h4>
							<ul class="space-y-1 text-sm text-gray-200 list-disc list-inside">
								{#each data.open_questions as q}<li>{q}</li>{/each}
							</ul>
						</section>
					{/if}
				</div>
				<p class="text-[11px] text-gray-600">{data.style} summary · {data.provider}/{data.model}</p>
			{/if}
			<button
				onclick={copyToClipboard}
				class="absolute top-0 right-0 text-gray-500 hover:text-gray-300 text-xs px-2 py-1 rounded bg-gray-800 border border-gray-700 transition-colors"
				title="Copy summary as markdown"
			>
				Copy
			</button>
		</div>
	{/if}

	<div class="flex flex-wrap items-center gap-3">
		<select bind:value={selectedStyle} class="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200" title="Summary style">
			{#each styles as s}
				<option value={s.id}>{s.id}</option>
			{/each}
		</select>
		<select bind:value={selectedProvider} onchange={handleProviderChange} class="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200">
			{#each providers as p}
				<option value={p.provider}>{p.provider}</option>
			{/each}
		</select>
		<select bind:value={selectedModel} class="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 min-w-[200px]">
			{#each availableModels() as model}
				<option value={model}>{model}</option>
			{/each}
			{#if availableModels().length === 0}
				<option value="">No models available</option>
			{/if}
		</select>
		<button
			onclick={handleSummarize}
			disabled={!!activeJob || !sessionState.activeSession?.transcript?.length}
			class="px-4 py-1.5 text-sm rounded bg-purple-600 hover:bg-purple-700 disabled:bg-gray-700 disabled:text-gray-500 text-white font-medium transition-colors"
		>
			{activeJob ? 'Summarizing...' : sessionState.activeSession?.summary ? 'Re-summarize' : 'Summarize'}
		</button>
	</div>

	{#if activeJob}
		<p class="flex items-center gap-2 text-sm text-gray-400">
			<span class="w-2 h-2 rounded-full bg-purple-500 animate-pulse"></span>
			{activeJob.status === 'queued' ? 'Queued...' : activeJob.message || 'Summarizing...'}
			<span class="text-gray-600">· you can keep working; the summary appears here when ready</span>
		</p>
	{/if}

	{#if error || jobError}
		<p class="text-red-400 text-sm">{error || jobError}</p>
	{/if}

	{#if !sessionState.activeSession?.transcript?.length}
		<p class="text-gray-600 text-sm">Record and transcribe audio first to generate a summary.</p>
	{/if}
</div>
