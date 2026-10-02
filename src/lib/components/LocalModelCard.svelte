<script lang="ts">
	import { downloadLocalModel, getLocalModel, pullOllamaModel, removeLocalModel, updateSettings } from '$lib/api/backend.js';
	import { jobsState } from '$lib/stores/jobs.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { LocalModelStatus } from '$lib/types/index.js';
	import JobProgress from './JobProgress.svelte';

	// Summaries with nothing else installed (backend services/local_llm.py): the built-in model,
	// downloaded once; or, when Ollama runs, a model pulled into it. `onReady` hears the provider
	// and model to use once one is there (the wizard selects it; Settings saves it).
	let {
		onReady,
		ollama = false,
		builtin = true
	}: { onReady?: (provider: 'local' | 'ollama', model: string) => void; ollama?: boolean; builtin?: boolean } = $props();

	let status = $state<LocalModelStatus | null>(null);
	let jobId = $state<string | null>(null);
	let pullId = $state<string | null>(null);
	const job = $derived(jobId ? (jobsState.jobs[jobId] ?? null) : null);
	const pull = $derived(pullId ? (jobsState.jobs[pullId] ?? null) : null);

	async function load() {
		try {
			status = await getLocalModel();
		} catch {
			status = null;
		}
	}

	$effect(() => {
		load();
		return jobsState.onComplete((j) => {
			if (j.id === jobId || j.id === pullId) {
				const model = (j.result as { model?: string } | null)?.model ?? '';
				void load();
				if (j.id === jobId) onReady?.('local', model);
				else onReady?.('ollama', model);
				jobId = pullId = null;
			}
		});
	});

	$effect(() => {
		if (job?.status === 'failed') {
			toastState.error(job.error ?? 'The download failed');
			jobId = null;
		}
		if (pull?.status === 'failed') {
			toastState.error(pull.error ?? 'Ollama could not pull the model');
			pullId = null;
		}
	});

	async function download(id: string) {
		try {
			const j = await downloadLocalModel(id);
			jobsState.track(j);
			jobId = j.id;
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not start the download');
		}
	}

	async function pullIntoOllama(model: string) {
		try {
			const j = await pullOllamaModel(model);
			jobsState.track(j);
			pullId = j.id;
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not pull the model');
		}
	}

	async function remove(id: string) {
		if (!confirm('Delete this model from disk? You can download it again later.')) return;
		await removeLocalModel(id);
		await load();
	}

	async function use(id: string) {
		await updateSettings({ default_provider: 'local', default_model: id });
		toastState.success('Summaries now use the built-in model');
		onReady?.('local', id);
	}

	const gb = (bytes: number) => `${(bytes / 1e9).toFixed(1)} GB`;
</script>

{#if status}
	<div class="space-y-2 text-sm">
		{#if ollama && status.ollama.reachable}
			<div class="rounded border border-gray-800 bg-gray-900/60 p-2">
				{#if status.ollama.models.length}
					<p class="text-gray-300">Ollama is running with {status.ollama.models.length} model{status.ollama.models.length === 1 ? '' : 's'}.</p>
				{:else}
					<p class="text-gray-300">Ollama is running but has no model yet.</p>
					{#if pull}
						<JobProgress job={pull} />
					{:else}
						<button onclick={() => pullIntoOllama(status!.ollama.suggested)} class="mt-1 px-3 py-1 rounded bg-blue-600 hover:bg-blue-500 text-white text-xs">
							Pull {status.ollama.suggested} into Ollama
						</button>
					{/if}
				{/if}
			</div>
		{/if}
		{#if builtin}
		<p class="text-xs text-gray-500">
			Built in: runs on this computer ({status.vulkan ? 'your graphics card through Vulkan, else the CPU' : 'the CPU'}; {status.ram_gb} GB of RAM), nothing else to install. Downloaded once; on a CPU a long meeting takes a few minutes to summarize.
		</p>
		<ul class="space-y-1">
			{#each status.models as m (m.id)}
				<li class="flex flex-wrap items-center gap-2">
					<span class="text-gray-200">{m.label}</span>
					{#if m.recommended}<span class="rounded bg-blue-900/50 px-1.5 text-xs text-blue-200">best for this computer</span>{/if}
					<span class="ml-auto flex items-center gap-2">
						{#if m.downloaded}
							<span class="text-xs text-green-400">downloaded</span>
							<button onclick={() => use(m.id)} class="text-xs text-blue-400 hover:text-blue-300">Use for summaries</button>
							<button onclick={() => remove(m.id)} class="text-xs text-gray-500 hover:text-red-400">Delete</button>
						{:else if !job}
							<button onclick={() => download(m.id)} class="px-2 py-0.5 rounded border border-gray-700 bg-gray-800 text-xs text-gray-200 hover:bg-gray-700">
								Download {gb(m.size)}
							</button>
						{/if}
					</span>
				</li>
			{/each}
		</ul>
		{#if job}<JobProgress {job} />{/if}
		{/if}
	</div>
{/if}
