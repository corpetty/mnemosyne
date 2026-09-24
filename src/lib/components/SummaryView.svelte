<script module lang="ts">
	import type { ProviderModels as PM, SummaryStyle as SS } from '$lib/types/index.js';
	// Survives tab switches so the pickers do not flash empty while models are re-listed.
	let cachedProviders: PM[] | null = null;
	let cachedStyles: SS[] | null = null;
</script>

<script lang="ts">
	import { listModels, listSummaryStyles, summarizeSession, getSettings } from '$lib/api/backend.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { jobsState } from '$lib/stores/jobs.svelte.js';
	import { playerState } from '$lib/stores/player.svelte.js';
	import Markdown from './Markdown.svelte';
	import { createGitHubIssues, setActionItemDone } from '$lib/api/backend.js';

	let selected = $state<Set<number>>(new Set());
	let creating = $state(false);
	let lastSession: string | null = null;
	$effect(() => {
		const id = sessionState.activeSession?.id ?? null;
		if (id !== lastSession) {
			lastSession = id;
			selected = new Set();
		}
	});

	async function toggleDone(i: number, done: boolean) {
		const session = sessionState.activeSession;
		if (!session) return;
		try {
			await setActionItemDone(session.id, i, done);
			await sessionState.refreshActive();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not update the item');
		}
	}

	function toggle(i: number) {
		const next = new Set(selected);
		if (next.has(i)) next.delete(i);
		else next.add(i);
		selected = next;
	}

	function fmtTime(sec: number): string {
		const m = Math.floor(sec / 60);
		const s = Math.floor(sec % 60);
		return m >= 60
			? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}:${String(s).padStart(2, '0')}`
			: `${m}:${String(s).padStart(2, '0')}`;
	}

	/** Jump to the transcript line where a chapter starts (and play from there if audio exists). */
	function openChapter(start: number) {
		const session = sessionState.activeSession;
		if (!session) return;
		const idx = session.transcript.findIndex((seg) => seg.start >= start - 0.01);
		sessionState.pendingOpen = { sessionId: session.id, idx: idx < 0 ? null : idx };
		if (playerState.available) playerState.seek(start, false);
	}

	async function openLink(href: string) {
		try {
			const { invoke } = await import('@tauri-apps/api/core');
			await invoke('plugin:shell|open', { path: href });
		} catch {
			window.open(href, '_blank', 'noopener,noreferrer');
		}
	}

	async function createIssues() {
		const session = sessionState.activeSession;
		if (!session || selected.size === 0) return;
		const settings = await getSettings();
		const repo = settings.values.github_repo;
		if (!repo) {
			toastState.error('Set a GitHub repository and token in Settings first');
			return;
		}
		const n = selected.size;
		if (!confirm(`Create ${n} issue${n > 1 ? 's' : ''} in ${repo}?`)) return;
		creating = true;
		try {
			const r = await createGitHubIssues(session.id, [...selected].sort((a, b) => a - b));
			selected = new Set();
			await sessionState.refreshActive();
			if (r.created.length) toastState.success(`Created ${r.created.length} issue${r.created.length > 1 ? 's' : ''} in ${repo}`);
			for (const err of r.errors) toastState.error(err);
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not create issues');
		} finally {
			creating = false;
		}
	}
	import type { ProviderModels, SummaryStyle } from '$lib/types/index.js';

	let providers = $state<ProviderModels[]>(cachedProviders ?? []);
	let styles = $state<SummaryStyle[]>(cachedStyles ?? []);
	let optionsLoading = $state(cachedProviders === null);
	let selectedProvider = $state('');
	let selectedModel = $state('');
	let selectedStyle = $state('');
	let error = $state('');
	let loaded = $state(false);

	async function load() {
		try {
			const [p, s, settings] = await Promise.all([listModels(), listSummaryStyles(), getSettings()]);
			providers = cachedProviders = p;
			styles = cachedStyles = s;
			optionsLoading = false;
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
				<Markdown text={sessionState.activeSession.summary} />
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
								{#each data.action_items as a, i}
									<li class="flex gap-2 items-start">
										{#if a.issue_url}
											<a href={a.issue_url} target="_blank" rel="noopener noreferrer" onclick={(e) => { e.preventDefault(); openLink(a.issue_url!); }} class="text-green-500 hover:text-green-300 text-xs mt-0.5" title="Open the GitHub issue">✓ issue</a>
										{:else}
											<input type="checkbox" checked={selected.has(i)} onchange={() => toggle(i)} class="mt-1 rounded border-gray-600 bg-gray-800" title="Select to create a GitHub issue" />
										{/if}
										<button
											onclick={() => toggleDone(i, !a.done)}
											title={a.done ? 'Done · click to reopen' : 'Mark done'}
											aria-label={a.done ? `Reopen: ${a.text}` : `Mark done: ${a.text}`}
											class="text-xs mt-0.5 {a.done ? 'text-emerald-400' : 'text-gray-600 hover:text-gray-300'}"
										>{a.done ? '✓' : '○'}</button>
										<span class={a.done ? 'line-through text-gray-500' : ''}>{a.text}{#if a.owner}<span class="text-gray-500"> · {a.owner}</span>{/if}</span>
									</li>
								{/each}
							</ul>
							{#if selected.size > 0}
								<button onclick={createIssues} disabled={creating} class="mt-2 px-3 py-1 text-xs rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200 disabled:opacity-50">
									{creating ? 'Creating…' : `Create ${selected.size} GitHub issue${selected.size > 1 ? 's' : ''}`}
								</button>
							{/if}
						</section>
					{/if}
					{#if data.chapters.length}
						<section class="bg-gray-900 border border-gray-700 rounded-lg p-3 md:col-span-2">
							<h4 class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-2">Chapters</h4>
							<ol class="space-y-0.5 text-sm">
								{#each data.chapters as c (c.start)}
									<li>
										<button onclick={() => openChapter(c.start)} class="flex gap-3 text-left text-gray-200 hover:text-white">
											<span class="font-mono text-xs text-blue-400 pt-0.5 w-12 text-right">{fmtTime(c.start)}</span>
											<span>{c.title}</span>
										</button>
									</li>
								{/each}
							</ol>
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
				<option value="">{optionsLoading ? 'Loading models…' : 'No models available'}</option>
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

	{#if sessionState.activeSession?.summary_stale && !activeJob}
		<p class="text-xs text-yellow-500">
			The transcript changed (text or speaker names) since this summary was made. Re-summarize to update it.
		</p>
	{/if}

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
