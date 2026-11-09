<script lang="ts">
	// A published meeting, read-only. Takes the decrypted manifest, so the app's Publish dialog
	// can render its preview (`preview`: the footer says so) with this same component. `updated`
	// ("14:32") and `status` (how the page keeps up, live.ts STATUS) come from the viewer only.
	// `comments` (readers' comments are open, comments.ts) adds a "Comment" button, or "n comments",
	// to each decision, question, action item and quote, and a floating "Comment" over a passage
	// selected in the summary; `highlight` briefly tints the part a thread's "On: …" line named.
	import Markdown from '../../src/lib/components/Markdown.svelte';
	import {
		commentCount,
		itemAnchor,
		passageAnchor,
		quoteAnchor,
		type Draft,
		type ItemTarget,
		type MeetingComments,
		type OpAnchor
	} from './comments';
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
		status = null,
		comments = null,
		highlight = null
	}: {
		manifest: Manifest;
		loadClip: ClipLoader;
		loadImage?: ImageLoader;
		preview?: boolean;
		updated?: string | null;
		status?: string | null;
		comments?: MeetingComments | null;
		/** The `anchorKey` of the part to tint for a moment. */
		highlight?: string | null;
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

	/** The tint of a part a thread pointed at (its `transition-colors` fades it in and out). */
	const tint = (key: string) => (highlight === key ? 'bg-sky-950/40' : '');
	const item = (target: ItemTarget, x: { id: string; text: string }): Draft => ({ anchor: itemAnchor(target, x.id), line: x.text });

	// ---- a passage of the summary or the notes, selected -----------------------------------------
	let summarySection = $state<HTMLElement | null>(null);
	let summaryBox = $state<HTMLElement | null>(null);
	let notesSection = $state<HTMLElement | null>(null);
	/** Where the floating "Comment" goes (px in its section), which part it is on, and what it anchors. */
	let pick = $state<{ in: 'body' | 'notes'; x: number; y: number; anchor: OpAnchor } | null>(null);
	/** The notes take comments only when the owner's META says so (an older owner, no sidecar: no). */
	const notesOpen = $derived(!!comments && comments.notes === true);

	function readSelection() {
		const sel = typeof document.getSelection === 'function' ? document.getSelection() : null;
		if (!comments || !sel || sel.isCollapsed || sel.rangeCount === 0) {
			pick = null;
			return;
		}
		const range = sel.getRangeAt(0);
		const parts = [
			{ in: 'body' as const, section: summarySection, body: summaryBox },
			{ in: 'notes' as const, section: notesSection, body: notesOpen ? notesBox : null }
		];
		// Both ends of the selection inside the same box.
		const hit = parts.find((p) => p.section && p.body && p.body.contains(range.startContainer) && p.body.contains(range.endContainer));
		if (!hit || !hit.section || !hit.body) {
			pick = null;
			return;
		}
		const { section, body } = hit;
		const before = document.createRange();
		before.selectNodeContents(body);
		before.setEnd(range.startContainer, range.startOffset);
		const start = before.toString().length;
		const anchor = passageAnchor(body.textContent ?? '', start, start + range.toString().length, hit.in);
		if (!anchor) {
			pick = null;
			return;
		}
		const rects = typeof range.getClientRects === 'function' ? range.getClientRects() : null;
		const end = rects && rects.length ? rects[rects.length - 1] : null;
		const box = section.getBoundingClientRect();
		const x = end ? end.right - box.left : box.width / 2;
		pick = { in: hit.in, x: Math.max(0, Math.min(box.width, x)), y: end ? end.top - box.top : 0, anchor };
	}

	function commentOnPassage() {
		const p = pick;
		if (!p || !comments) return;
		pick = null;
		document.getSelection?.()?.removeAllRanges();
		comments.onComment({ anchor: p.anchor, line: p.anchor.exact });
	}
</script>

<svelte:document onselectionchange={readSelection} />

{#snippet passageButton(part: 'body' | 'notes')}
	{#if pick && pick.in === part && comments}
		<button
			type="button"
			class="absolute z-10 -translate-x-1/2 -translate-y-full rounded border border-gray-700 bg-gray-900 px-2 py-1 text-xs"
			style="left: {pick.x}px; top: {pick.y - 4}px"
			onmousedown={(e) => e.preventDefault()}
			onclick={commentOnPassage}
			data-testid="comment-passage">Comment</button
		>
	{/if}
{/snippet}

{#snippet affordance(key: string, draft: Draft)}
	{#if comments}
		{@const n = comments.counts[key] ?? 0}
		{#if n}
			<button type="button" class="ml-auto shrink-0 text-xs text-sky-400" onclick={() => comments?.onShow(key)} data-testid="comment-count"
				>{commentCount(n)}</button
			>
		{:else}
			<button
				type="button"
				class="ml-auto shrink-0 text-xs text-gray-500 hover:text-gray-300 md:opacity-0 md:group-focus-within:opacity-100 md:group-hover:opacity-100"
				onclick={() => comments?.onComment(draft)}
				data-testid="comment-on">Comment</button
			>
		{/if}
	{/if}
{/snippet}

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
		<section bind:this={summarySection} class="relative rounded transition-colors {tint('body')}" data-anchor="body">
			<div bind:this={summaryBox}><Markdown text={m.summary} /></div>
			{@render passageButton('body')}
		</section>
	{/if}

	{#if decisions.length}
		<section>
			<h2 class="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">Decisions</h2>
			<ul class="space-y-1 text-sm text-gray-200">
				{#each decisions as d (d.id)}
					<li class="group flex gap-2 rounded transition-colors {tint(`decision:${d.id}`)}" data-anchor="decision:{d.id}">
						<span class="shrink-0 text-gray-500" aria-hidden="true">{'\u2714\ufe0e'}</span><span>{d.text}</span>
						{@render affordance(`decision:${d.id}`, item('decision', d))}
					</li>
				{/each}
			</ul>
		</section>
	{/if}

	{#if questions.length}
		<section>
			<h2 class="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">Open questions</h2>
			<ul class="space-y-1 text-sm text-gray-200">
				{#each questions as q (q.id)}
					<li class="group flex gap-2 rounded transition-colors {tint(`question:${q.id}`)}" data-anchor="question:{q.id}">
						<span class="w-3 shrink-0 text-center text-gray-500" aria-hidden="true">?</span><span>{q.text}</span>
						{@render affordance(`question:${q.id}`, item('question', q))}
					</li>
				{/each}
			</ul>
		</section>
	{/if}

	{#if actions.length}
		<section>
			<h2 class="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">Action items</h2>
			<ul class="space-y-1 text-sm text-gray-200">
				{#each actions as a (a.id)}
					<li class="group flex gap-2 rounded transition-colors {tint(`item:${a.id}`)}" data-anchor="item:{a.id}">
						<span class="shrink-0 text-gray-500" aria-hidden="true">{a.done ? '✓' : '○'}</span>
						<!-- Svelte trims whitespace at the edges of an element, so the separators are expressions. -->
						<span class={a.done ? 'text-gray-500 line-through' : ''}
							>{a.text}{#if a.owner}<span class="text-gray-500">{' · '}{a.owner}</span>{/if}{#if a.due}<span
									class="text-xs text-gray-500">{' · due '}{formatDate(a.due)}</span
								>{/if}{#if a.done}<span class="sr-only">{' (done)'}</span>{/if}</span
						>
						{@render affordance(`item:${a.id}`, item('item', a))}
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
		<section bind:this={notesSection} class="relative rounded transition-colors {tint('notes')}" data-anchor="notes">
			<div class="mb-2 flex items-baseline gap-2">
				<h2 class="text-xs font-semibold uppercase tracking-wide text-gray-500">Notes</h2>
				{#if comments && notesOpen && (comments.counts.notes ?? 0) > 0}
					<button type="button" class="ml-auto shrink-0 text-xs text-sky-400" onclick={() => comments?.onShow('notes')} data-testid="comment-count"
						>{commentCount(comments.counts.notes)}</button
					>
				{/if}
			</div>
			<div bind:this={notesBox}><Markdown text={notes.markdown} /></div>
			{@render passageButton('notes')}
		</section>
	{/if}

	{#if quotes.length}
		<section>
			<h2 class="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">Quotes</h2>
			<div class="space-y-4">
				{#each quotes as q (q.id)}
					<div class="group rounded transition-colors {tint(`quote:${q.id}`)}" data-anchor="quote:{q.id}">
						<Quote quote={q} {loadClip}>
							{#snippet extra()}
								{@render affordance(`quote:${q.id}`, { anchor: quoteAnchor(q.id), line: q.text })}
							{/snippet}
						</Quote>
					</div>
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
