<script lang="ts">
	import { digestState } from '$lib/stores/digest.svelte.js';
	import { jobsState } from '$lib/stores/jobs.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { Digest } from '$lib/types/index.js';
	import Markdown from './Markdown.svelte';

	let { onOpenSession }: { onOpenSession?: () => void } = $props();

	function iso(d: Date): string {
		return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
	}

	function weekOf(offset: number): [string, string] {
		const d = new Date();
		d.setDate(d.getDate() - ((d.getDay() + 6) % 7) + offset * 7);
		const end = new Date(d);
		end.setDate(d.getDate() + 6);
		return [iso(d), iso(end)];
	}

	let [start, end] = $state(weekOf(0));
	let submitting = $state(false);

	$effect(() => {
		if (!digestState.loaded) digestState.load();
	});

	$effect(() => {
		void jobsState.jobs;
		digestState.syncFailure();
	});

	function setWeek(offset: number) {
		[start, end] = weekOf(offset);
	}

	async function generate() {
		if (submitting || !start || !end) return;
		submitting = true;
		try {
			await digestState.generate(start, end);
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not start the digest');
		} finally {
			submitting = false;
		}
	}

	/** [[note|Name]] wiki links become in-page links that open the meeting. */
	function linkMeetings(d: Digest): string {
		const ids = new Set(d.session_ids);
		return d.markdown.replace(/\[\[([^\]|]+)\|([^\]]+)\]\]/g, (_whole, stem: string, name: string) => {
			const day = stem.slice(0, 10);
			const s = sessionState.sessions.find(
				(x) => ids.has(x.id) && x.name === name && iso(new Date(x.created_at)) === day
			);
			return s ? `[${name}](#open-${s.id})` : name;
		});
	}

	async function onAnchor(id: string) {
		const m = id.match(/^open-(.+)$/);
		if (!m) return;
		onOpenSession?.();
		await sessionState.selectSession(m[1]);
	}

	async function copy(d: Digest) {
		await navigator.clipboard.writeText(d.markdown);
		toastState.info('Digest copied');
	}

	function fmtRange(d: Digest): string {
		const f = (x: string) =>
			new Date(`${x}T00:00`).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
		return `${f(d.start)} – ${f(d.end)}`;
	}

	const pending = $derived(digestState.pendingJob ? jobsState.jobs[digestState.pendingJob] : null);
	const current = $derived(digestState.selected);
</script>

<div class="space-y-5">
	<div>
		<h2 class="text-xl font-semibold text-gray-100 mb-1">Digest</h2>
		<p class="text-xs text-gray-500">
			One note for a week of meetings: an overview and themes written by the default LLM, plus every decision
			and action item copied from the summaries. Saved to your Obsidian vault under <code>digests/</code> when a
			vault is set. Only summarized meetings are included.
		</p>
	</div>

	<div class="flex flex-wrap items-end gap-3">
		<label class="text-xs text-gray-500 space-y-1">
			<span class="block">From</span>
			<input type="date" bind:value={start} class="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-200" />
		</label>
		<label class="text-xs text-gray-500 space-y-1">
			<span class="block">To</span>
			<input type="date" bind:value={end} class="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-200" />
		</label>
		<div class="flex gap-1 pb-0.5">
			<button onclick={() => setWeek(0)} class="px-2 py-1 text-xs rounded text-gray-400 hover:text-gray-200 hover:bg-gray-800">This week</button>
			<button onclick={() => setWeek(-1)} class="px-2 py-1 text-xs rounded text-gray-400 hover:text-gray-200 hover:bg-gray-800">Last week</button>
		</div>
		<button
			onclick={generate}
			disabled={submitting || !!pending || !start || !end}
			class="px-4 py-1.5 text-sm rounded bg-purple-600 hover:bg-purple-700 disabled:bg-gray-700 disabled:text-gray-500 text-white font-medium transition-colors"
		>
			{pending ? 'Writing…' : 'Generate'}
		</button>
	</div>

	{#if pending}
		<p class="flex items-center gap-2 text-sm text-gray-400">
			<span class="w-2 h-2 rounded-full bg-purple-500 animate-pulse"></span>
			{pending.message || 'Queued...'}
		</p>
	{/if}
	{#if digestState.error}
		<p class="text-red-400 text-sm">{digestState.error}</p>
	{/if}

	{#if digestState.history.length > 1}
		<div class="flex flex-wrap gap-1">
			{#each digestState.history as d (d.id)}
				<button
					onclick={() => (digestState.selectedId = d.id)}
					class="px-2 py-0.5 text-xs rounded-full border transition-colors
						{current?.id === d.id ? 'bg-gray-700 border-gray-600 text-gray-100' : 'border-gray-800 text-gray-400 hover:text-gray-200'}"
					title={fmtRange(d)}
				>
					{d.label}
				</button>
			{/each}
		</div>
	{/if}

	{#if current}
		<article class="rounded-lg border border-gray-800 bg-gray-900/60 p-4 space-y-3">
			<header class="flex items-start justify-between gap-3 text-[11px] text-gray-500">
				<span>{fmtRange(current)} · {current.session_ids.length} meeting{current.session_ids.length === 1 ? '' : 's'}</span>
				<div class="flex items-center gap-3 flex-shrink-0">
					<button onclick={() => copy(current)} class="hover:text-gray-300">Copy</button>
					<button onclick={() => digestState.remove(current.id)} class="hover:text-red-400" title="Delete">✕</button>
				</div>
			</header>
			<Markdown text={linkMeetings(current)} {onAnchor} />
			<p class="text-[10px] text-gray-700">
				{current.provider}/{current.model}{#if current.path} · saved to {current.path}{/if}
			</p>
		</article>
	{:else if digestState.loaded && !pending}
		<p class="text-sm text-gray-600">No digests yet. A weekly one can also run on a schedule (Settings → Digest).</p>
	{/if}
</div>
