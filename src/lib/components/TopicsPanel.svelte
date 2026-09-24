<script lang="ts">
	import { getThread, listTopics, summarizeThread } from '$lib/api/backend.js';
	import { jobsState } from '$lib/stores/jobs.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { Thread, TopicCount } from '$lib/types/index.js';
	import Markdown from './Markdown.svelte';

	let { onOpenSession }: { onOpenSession?: () => void } = $props();

	let topics = $state<TopicCount[]>([]);
	let query = $state('');
	let thread = $state<Thread | null>(null);
	let loading = $state(false);
	let jobId = $state<string | null>(null);
	let status = $state('');

	$effect(() => {
		listTopics()
			.then((t) => (topics = t))
			.catch(() => {});
	});

	async function follow(q: string) {
		q = q.trim();
		if (q.length < 2) return;
		query = q;
		loading = true;
		status = '';
		jobId = null;
		try {
			thread = await getThread(q);
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not load the topic');
		} finally {
			loading = false;
		}
	}

	async function explain() {
		if (!thread) return;
		try {
			const job = await summarizeThread(thread.query);
			jobsState.track(job);
			jobId = job.id;
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not start');
		}
	}

	const job = $derived(jobId ? jobsState.jobs[jobId] : null);
	const explanation = $derived(job?.status === 'completed' ? ((job.result as { text?: string } | null)?.text ?? '') : '');

	async function open(id: string, start?: number) {
		const s = sessionState.sessions.find((x) => x.id === id);
		if (!s) return;
		onOpenSession?.();
		await sessionState.selectSession(id);
		if (start !== undefined && sessionState.activeSession) {
			const idx = sessionState.activeSession.transcript.findIndex((seg) => seg.start >= start - 0.01);
			sessionState.pendingOpen = { sessionId: id, idx: idx < 0 ? null : idx };
		}
	}

	function day(iso: string): string {
		return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
	}

	function mmss(sec: number): string {
		return `${Math.floor(sec / 60)}:${String(Math.floor(sec % 60)).padStart(2, '0')}`;
	}
</script>

<div class="space-y-5">
	<div>
		<h2 class="text-xl font-semibold text-gray-100 mb-1">Topics</h2>
		<p class="text-xs text-gray-500">Follow one subject across meetings: what was said, decided and left open, oldest first.</p>
	</div>

	<form onsubmit={(e) => { e.preventDefault(); follow(query); }} class="flex gap-2">
		<input
			bind:value={query}
			placeholder="e.g. Waku migration, hiring, infra costs"
			aria-label="Topic"
			class="flex-1 bg-gray-900 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-100 placeholder-gray-600"
		/>
		<button type="submit" class="px-4 py-1.5 text-sm rounded bg-blue-600 hover:bg-blue-700 text-white font-medium">Follow</button>
	</form>

	{#if topics.length}
		<div class="flex flex-wrap gap-1">
			{#each topics as t (t.topic)}
				<button
					onclick={() => follow(t.topic)}
					class="px-2 py-0.5 rounded-full border text-xs {thread?.query === t.topic ? 'bg-gray-700 border-gray-600 text-gray-100' : 'border-gray-800 text-gray-400 hover:text-gray-200'}"
					title="{t.meetings} meetings, last {day(t.last_seen)}"
				>{t.topic}{#if t.meetings > 1}<span class="text-gray-600"> {t.meetings}</span>{/if}</button>
			{/each}
		</div>
	{/if}

	{#if loading}
		<p class="text-sm text-gray-500">Looking…</p>
	{:else if thread}
		{#if thread.meetings.length === 0}
			<p class="text-sm text-gray-600">No meetings about “{thread.query}”.</p>
		{:else}
			<div class="flex items-center gap-3">
				<button
					onclick={explain}
					disabled={job?.status === 'queued' || job?.status === 'running'}
					class="px-3 py-1 text-xs rounded bg-purple-600 hover:bg-purple-700 disabled:bg-gray-700 text-white font-medium"
				>
					{job?.status === 'queued' || job?.status === 'running' ? 'Reading…' : 'Where does this stand?'}
				</button>
				<span class="text-xs text-gray-500">{thread.meetings.length} meeting{thread.meetings.length === 1 ? '' : 's'}</span>
				{#if job?.status === 'failed'}<span class="text-xs text-red-400">{job.error}</span>{/if}
			</div>
			{#if explanation}
				<div class="rounded-lg border border-purple-900/60 bg-purple-950/20 p-4" aria-label="Where it stands">
					<Markdown text={explanation} />
				</div>
			{/if}
			<ol class="relative border-l border-gray-800 ml-2 space-y-5">
				{#each thread.meetings as m (m.id)}
					<li class="ml-4">
						<span class="absolute -left-1.5 mt-1.5 h-3 w-3 rounded-full border border-gray-700 bg-gray-900"></span>
						<p class="text-xs text-gray-500">{day(m.created_at)}</p>
						<button onclick={() => open(m.id)} class="text-sm font-medium text-gray-100 hover:underline text-left">{m.name}</button>
						{#if m.summary}<p class="text-xs text-gray-400 mt-0.5">{m.summary}</p>{/if}
						<ul class="mt-1 space-y-0.5 text-sm">
							{#each m.chapters as c (c.start)}
								<li><button onclick={() => open(m.id, c.start)} class="text-blue-300 hover:text-blue-200 text-left">▸ {mmss(c.start)} {c.title}</button></li>
							{/each}
							{#each m.decisions as d}<li class="text-gray-200">✔ {d}</li>{/each}
							{#each m.action_items as a (a.idx)}
								<li class={a.done ? 'text-gray-500 line-through' : 'text-gray-300'}>○ {a.text}{#if a.owner}<span class="text-gray-500"> · {a.owner}</span>{/if}</li>
							{/each}
							{#each m.open_questions as q}<li class="text-gray-400">? {q}</li>{/each}
						</ul>
					</li>
				{/each}
			</ol>
		{/if}
	{:else if topics.length === 0}
		<p class="text-sm text-gray-600">Topics come from meeting summaries. Type any subject to follow it.</p>
	{/if}
</div>
