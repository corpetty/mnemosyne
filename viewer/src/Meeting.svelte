<script lang="ts">
	// A published meeting, read-only. Takes the decrypted manifest, so the app's Publish dialog
	// can render its preview (`preview`: the footer says so) with this same component.
	import Markdown from '../../src/lib/components/Markdown.svelte';
	import { footerLine, formatClock, formatDate, metaLine } from './display';
	import type { Manifest } from './format';
	import type { ClipLoader } from './load';
	import Quote from './Quote.svelte';

	let { manifest, loadClip, preview = false }: { manifest: Manifest; loadClip: ClipLoader; preview?: boolean } = $props();

	const m = $derived(manifest);
	const meta = $derived(metaLine(m.date, m.duration_seconds, m.attendees ?? []));
	const decisions = $derived(m.decisions ?? []);
	const questions = $derived(m.questions ?? []);
	const actions = $derived(m.action_items ?? []);
	const chapters = $derived(m.chapters ?? []);
	const quotes = $derived(m.quotes ?? []);
	const topics = $derived(m.topics ?? []);
</script>

<article class="space-y-8">
	<header>
		<h1 class="text-xl font-semibold text-gray-100">{m.title}</h1>
		{#if meta}<p class="mt-1 text-sm text-gray-500">{meta}</p>{/if}
		{#if topics.length}
			<div class="mt-3 flex flex-wrap gap-1">
				{#each topics as t}
					<span class="rounded-full border border-gray-700 bg-gray-800 px-2 py-0.5 text-[11px] text-gray-400">{t}</span>
				{/each}
			</div>
		{/if}
	</header>

	{#if m.summary?.trim()}
		<section><Markdown text={m.summary} /></section>
	{/if}

	{#if decisions.length}
		<section>
			<h2 class="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">Decisions</h2>
			<ul class="space-y-1 text-sm text-gray-200">
				{#each decisions as d (d.id)}
					<li class="flex gap-2"><span class="shrink-0 text-gray-500" aria-hidden="true">{'\u2714\ufe0e'}</span><span>{d.text}</span></li>
				{/each}
			</ul>
		</section>
	{/if}

	{#if questions.length}
		<section>
			<h2 class="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">Open questions</h2>
			<ul class="space-y-1 text-sm text-gray-200">
				{#each questions as q (q.id)}
					<li class="flex gap-2"><span class="w-3 shrink-0 text-center text-gray-500" aria-hidden="true">?</span><span>{q.text}</span></li>
				{/each}
			</ul>
		</section>
	{/if}

	{#if actions.length}
		<section>
			<h2 class="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">Action items</h2>
			<ul class="space-y-1 text-sm text-gray-200">
				{#each actions as a (a.id)}
					<li class="flex gap-2">
						<span class="shrink-0 text-gray-500" aria-hidden="true">{a.done ? '✓' : '○'}</span>
						<!-- Svelte trims whitespace at the edges of an element, so the separators are expressions. -->
						<span class={a.done ? 'text-gray-500 line-through' : ''}
							>{a.text}{#if a.owner}<span class="text-gray-500">{' · '}{a.owner}</span>{/if}{#if a.due}<span
									class="text-xs text-gray-500">{' · due '}{formatDate(a.due)}</span
								>{/if}{#if a.done}<span class="sr-only">{' (done)'}</span>{/if}</span
						>
					</li>
				{/each}
			</ul>
		</section>
	{/if}

	{#if chapters.length}
		<section>
			<h2 class="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">Chapters</h2>
			<ol class="space-y-0.5 text-sm text-gray-200">
				{#each chapters as c (c.start)}
					<li class="flex gap-3">
						<span class="w-14 shrink-0 pt-0.5 text-right font-mono text-xs text-gray-500">{formatClock(c.start)}</span>
						<span>{c.title}</span>
					</li>
				{/each}
			</ol>
		</section>
	{/if}

	{#if m.notes?.trim()}
		<section>
			<h2 class="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">Notes</h2>
			<Markdown text={m.notes} />
		</section>
	{/if}

	{#if quotes.length}
		<section>
			<h2 class="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">Quotes</h2>
			<div class="space-y-4">
				{#each quotes as q (q.id)}
					<Quote quote={q} {loadClip} />
				{/each}
			</div>
		</section>
	{/if}

	<footer class="border-t border-gray-800 pt-4 text-xs text-gray-600">
		{footerLine(m.version, m.expires_at, preview)}
	</footer>
</article>
