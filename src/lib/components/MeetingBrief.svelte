<script lang="ts">
	import { getBrief } from '$lib/api/backend.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import type { Brief } from '$lib/types/index.js';
	import Markdown from './Markdown.svelte';

	/** What is still open from earlier meetings with this title or these people. */
	let {
		title,
		attendees = [],
		exclude,
		compact = false
	}: { title: string; attendees?: string[]; exclude?: string; compact?: boolean } = $props();

	let brief = $state<Brief | null>(null);
	let expanded = $state(false);

	$effect(() => {
		const key = [title, exclude, ...attendees];
		let cancelled = false;
		getBrief(title, attendees, exclude)
			.then((b) => {
				if (!cancelled) brief = b;
			})
			.catch(() => (brief = null));
		void key;
		return () => (cancelled = true);
	});

	function day(iso: string): string {
		return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
	}

	const last = $derived(brief?.meetings[0] ?? null);
	const open = $derived(compact ? expanded : true);
</script>

{#if brief && last}
	<div class="text-xs space-y-2 {compact ? '' : 'rounded-lg border border-gray-800 bg-gray-900/60 px-3 py-2'}" aria-label="Meeting brief">
		<div class="flex flex-wrap items-center gap-x-2 gap-y-1">
			<span class="{compact ? 'text-blue-300' : 'text-gray-400 font-medium'}">From earlier meetings:</span>
			<button onclick={() => sessionState.selectSession(last.id)} class="{compact ? 'text-blue-100' : 'text-gray-200'} hover:underline">
				{last.name}
			</button>
			<span class="{compact ? 'text-blue-300/70' : 'text-gray-600'}">· {day(last.created_at)}{#if brief.meetings.length > 1} and {brief.meetings.length - 1} before{/if}</span>
			<span class="{compact ? 'text-blue-200' : 'text-gray-400'}">
				· {brief.open_items.length} open item{brief.open_items.length === 1 ? '' : 's'}{#if brief.open_questions.length}, {brief.open_questions.length} open question{brief.open_questions.length === 1 ? '' : 's'}{/if}
			</span>
			{#if compact && (brief.open_items.length || brief.open_questions.length || brief.last_summary)}
				<button onclick={() => (expanded = !expanded)} class="text-blue-300 hover:text-blue-100">{expanded ? 'hide' : 'show'}</button>
			{/if}
		</div>
		{#if open}
			{#if brief.open_items.length}
				<ul class="space-y-0.5">
					{#each brief.open_items.slice(0, 10) as t (t.session_id + t.idx)}
						<li class="text-gray-300">
							○ {t.text}{#if t.owner}<span class="text-gray-500"> · {t.owner}</span>{/if}
							{#if t.session_id !== last.id}<span class="text-gray-600"> · {day(t.created_at)}</span>{/if}
						</li>
					{/each}
					{#if brief.open_items.length > 10}<li class="text-gray-600">… {brief.open_items.length - 10} more in Tasks</li>{/if}
				</ul>
			{/if}
			{#if brief.open_questions.length}
				<ul class="space-y-0.5">
					{#each brief.open_questions as q}
						<li class="text-gray-400">? {q.text}</li>
					{/each}
				</ul>
			{/if}
			{#if brief.last_summary && !compact}
				<details class="text-gray-400">
					<summary class="cursor-pointer text-gray-500 hover:text-gray-300">Last time's summary</summary>
					<div class="mt-1"><Markdown text={brief.last_summary} /></div>
				</details>
			{/if}
		{/if}
	</div>
{/if}
