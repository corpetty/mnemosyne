<script lang="ts">
	import { askState } from '$lib/stores/ask.svelte.js';
	import { jobsState } from '$lib/stores/jobs.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { Ask, Citation } from '$lib/types/index.js';
	import Markdown from './Markdown.svelte';

	let { onOpenSession }: { onOpenSession?: () => void } = $props();
	let input = $state<HTMLTextAreaElement>();
	let submitting = $state(false);

	$effect(() => {
		if (!askState.loaded) askState.load();
		input?.focus();
	});

	$effect(() => {
		// Re-evaluate whenever job state changes, to surface failures.
		void jobsState.jobs;
		askState.syncFailures();
	});

	async function submit() {
		const q = askState.draft.trim();
		if (!q || submitting) return;
		submitting = true;
		try {
			await askState.ask(q);
			askState.draft = '';
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not ask');
		} finally {
			submitting = false;
		}
	}

	function onKey(e: KeyboardEvent) {
		if (e.key === 'Enter' && !e.shiftKey) {
			e.preventDefault();
			submit();
		}
	}

	/** Turn [1] / [1, 3] markers into in-page links the Markdown component reports back. */
	function linkCitations(ask: Ask): string {
		const known = new Set(ask.citations.map((c) => c.n));
		return ask.answer.replace(/\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\]/g, (whole, list: string) => {
			const parts = list.split(',').map((x) => Number(x.trim()));
			if (!parts.every((n) => known.has(n))) return whole;
			return parts.map((n) => `[\\[${n}\\]](#cite-${ask.id}-${n})`).join('');
		});
	}

	async function openCitation(c: Citation) {
		sessionState.pendingOpen = { sessionId: c.session_id, idx: c.idx };
		onOpenSession?.();
		await sessionState.selectSession(c.session_id);
	}

	function onAnchor(ask: Ask, id: string) {
		const m = id.match(/^cite-[^-]+-(\d+)$/);
		const c = m ? ask.citations.find((x) => x.n === Number(m[1])) : null;
		if (c) openCitation(c);
	}

	function fmtTime(s: number | null): string {
		if (s === null) return 'summary';
		return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
	}

	function fmtDate(d: string): string {
		return new Date(d).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
	}

	const pendingList = $derived(Object.entries(askState.pending));
	const failedList = $derived(Object.entries(askState.failed));
</script>

<div class="space-y-5">
	<div>
		<h2 class="text-xl font-semibold text-gray-100 mb-1">Ask your meetings</h2>
		<p class="text-xs text-gray-500">
			Answers come only from your transcripts and summaries, with numbered sources that open the exact moment.
			Uses the default LLM provider from Settings.
		</p>
	</div>

	<div class="space-y-2">
		<textarea
			bind:this={input}
			bind:value={askState.draft}
			onkeydown={onKey}
			rows="2"
			placeholder="e.g. What did we decide about the Waku migration? Who owns the docs update?"
			class="w-full px-3 py-2 text-sm bg-gray-900 border border-gray-700 rounded-lg text-gray-100 placeholder-gray-600 resize-y focus:outline-none focus:border-gray-500"
		></textarea>
		<div class="flex items-center gap-3">
			<button
				onclick={submit}
				disabled={submitting || !askState.draft.trim()}
				class="px-4 py-1.5 text-sm rounded bg-blue-600 hover:bg-blue-700 disabled:bg-gray-700 disabled:text-gray-500 text-white font-medium transition-colors"
			>
				Ask
			</button>
			<span class="text-[11px] text-gray-600">Enter to ask · Shift+Enter for a new line</span>
		</div>
	</div>

	{#each failedList as [id, f] (id)}
		<div class="rounded-lg border border-red-900 bg-red-950/30 p-3 text-sm">
			<div class="flex justify-between gap-3">
				<p class="font-medium text-gray-200">{f.question}</p>
				<button onclick={() => askState.dismissFailure(id)} class="text-xs text-gray-500 hover:text-gray-300">dismiss</button>
			</div>
			<p class="text-red-300 text-xs mt-1">{f.error}</p>
		</div>
	{/each}

	{#each pendingList as [id, question] (id)}
		<div class="rounded-lg border border-gray-800 bg-gray-900/60 p-3">
			<p class="font-medium text-gray-200 text-sm">{question}</p>
			<p class="flex items-center gap-2 text-xs text-gray-500 mt-2">
				<span class="w-2 h-2 rounded-full bg-blue-500 animate-pulse"></span>
				{jobsState.jobs[id]?.message || 'Searching your meetings...'}
			</p>
		</div>
	{/each}

	{#each askState.history as ask (ask.id)}
		<article class="rounded-lg border border-gray-800 bg-gray-900/60 p-4 space-y-3">
			<header class="flex items-start justify-between gap-3">
				<h3 class="font-medium text-gray-100 text-sm">{ask.question}</h3>
				<div class="flex items-center gap-3 flex-shrink-0 text-[11px] text-gray-600">
					<span>{fmtDate(ask.created_at)}</span>
					<button onclick={() => askState.remove(ask.id)} class="hover:text-red-400" title="Delete">✕</button>
				</div>
			</header>
			<Markdown text={linkCitations(ask)} onAnchor={(id) => onAnchor(ask, id)} />
			{#if ask.citations.length}
				<div class="border-t border-gray-800 pt-2 space-y-1">
					{#each ask.citations as c (c.n)}
						<button
							onclick={() => openCitation(c)}
							class="w-full text-left text-[11px] leading-4 text-gray-400 hover:text-gray-200 hover:bg-gray-800/60 rounded px-1 py-0.5"
						>
							<span class="text-blue-400">[{c.n}]</span>
							<span class="text-gray-300">{c.session_name}</span>
							<span class="text-gray-600">· {fmtDate(c.created_at)} · {fmtTime(c.start)}</span>
							<span class="block text-gray-500 truncate">{c.excerpt}</span>
						</button>
					{/each}
				</div>
			{/if}
			{#if ask.model}
				<p class="text-[10px] text-gray-700">{ask.provider}/{ask.model}</p>
			{/if}
		</article>
	{/each}

	{#if askState.loaded && askState.history.length === 0 && pendingList.length === 0}
		<p class="text-sm text-gray-600">No questions yet.</p>
	{/if}
</div>
