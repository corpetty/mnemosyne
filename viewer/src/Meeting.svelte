<script lang="ts">
	// A published meeting, read-only. Takes the decrypted manifest, so the app's Publish dialog
	// can render its preview (`preview`: the footer says so) with this same component. `updated`
	// ("14:32") and `status` (how the page keeps up, live.ts STATUS) come from the viewer only.
	import Markdown from '../../src/lib/components/Markdown.svelte';
	import { footerLine, formatClock, formatDate, metaLine } from './display';
	import type { Manifest } from './format';
	import type { ClipLoader, ImageLoader } from './load';
	import { fillImages, prepareNotes } from './notes';
	import Quote from './Quote.svelte';

	let {
		manifest,
		loadClip,
		loadImage,
		preview = false,
		updated = null,
		status = null
	}: {
		manifest: Manifest;
		loadClip: ClipLoader;
		loadImage?: ImageLoader;
		preview?: boolean;
		updated?: string | null;
		status?: string | null;
	} = $props();

	const m = $derived(manifest);
	const meta = $derived(metaLine(m.date, m.duration_seconds, m.attendees ?? []));
	const decisions = $derived(m.decisions ?? []);
	const questions = $derived(m.questions ?? []);
	const actions = $derived(m.action_items ?? []);
	const chapters = $derived(m.chapters ?? []);
	const quotes = $derived(m.quotes ?? []);
	const topics = $derived(m.topics ?? []);

	// The notes' image lines become placeholders before the markdown renderer sees them (notes.ts),
	// and the pictures are put in once decrypted: a `mnemosyne:` address never reaches the page.
	const notes = $derived(prepareNotes(m.notes ?? '', m.images ?? []));
	let notesBox = $state<HTMLElement | null>(null);
	$effect(() => {
		if (!notesBox) return;
		// Read after the markdown has rendered into the box (child effects run first).
		return fillImages(notesBox, notes.slots, loadImage);
	});
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
			<div bind:this={notesBox}><Markdown text={notes.markdown} /></div>
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

	<!-- The status shares the footer's line when it fits, else wraps to above it. -->
	<footer class="flex flex-wrap-reverse justify-between gap-x-4 gap-y-1 border-t border-gray-800 pt-4 text-xs text-gray-600">
		<span>{footerLine(m.version, m.expires_at, preview, updated)}</span>
		{#if status}<span>{status}</span>{/if}
	</footer>
</article>

<style>
	/* The notes' pictures are put in from code, so their rules reach through Markdown's scoping. */
	:global(.mn-image) {
		box-sizing: border-box;
		max-width: 100%;
		margin: 0 0 0.6em;
		border-radius: 0.375rem;
		background: rgb(55 65 81);
	}
	:global(.mn-image-loaded) {
		background: none;
		width: auto;
	}
	:global(.mn-image img) {
		display: block;
		max-width: 100%;
		height: auto;
		border-radius: 0.375rem;
	}
	:global(.mn-image-missing) {
		margin: 0 0 0.6em;
		color: rgb(156 163 175);
	}
</style>
