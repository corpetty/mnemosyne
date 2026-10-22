<script lang="ts">
	import { commentsState } from '$lib/stores/comments.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { playerState } from '$lib/stores/player.svelte.js';
	import { uiState } from '$lib/stores/ui.svelte.js';
	import type { CommentThread as Thread } from '$lib/types/index.js';
	import CommentComposer from './CommentComposer.svelte';
	import CommentThread from './CommentThread.svelte';

	type Filter = 'open' | 'resolved' | 'all';

	/** Every thread of the open meeting, below the summary; `ongoto` opens a summary block's panel. */
	let { ongoto, filter = $bindable('open') }: { ongoto: (blockId: string) => void; filter?: Filter } = $props();

	let composing = $state(false);

	const session = $derived(sessionState.activeSession);
	const data = $derived(session?.summary_data ?? null);

	const shown = $derived(
		commentsState.threads.filter((t) =>
			filter === 'all' ? true : filter === 'resolved' ? !!t.root.resolved_at : !t.root.resolved_at
		)
	);

	function clip(s: string, n: number): string {
		const t = s.replace(/\s+/g, ' ').trim();
		return t.length > n ? `${t.slice(0, n)}…` : t;
	}

	function fmtTime(sec: number): string {
		const m = Math.floor(sec / 60);
		const s = Math.floor(sec % 60);
		return m >= 60
			? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}:${String(s).padStart(2, '0')}`
			: `${m}:${String(s).padStart(2, '0')}`;
	}

	/** The block's current text, else what it said when the thread was started. */
	function blockText(t: Thread): string {
		const id = t.root.target_id;
		if (data) {
			if (t.root.target === 'decision') {
				const i = data.decision_ids?.indexOf(id) ?? -1;
				if (i >= 0 && data.decisions[i]) return data.decisions[i];
			} else if (t.root.target === 'question') {
				const i = data.question_ids?.indexOf(id) ?? -1;
				if (i >= 0 && data.open_questions[i]) return data.open_questions[i];
			} else if (t.root.target === 'item') {
				const a = data.action_items.find((x) => x.id === id);
				if (a) return a.text;
			}
		}
		return t.root.quote;
	}

	/** What a thread is on, for the muted line above it. */
	function onWhat(t: Thread): string {
		const r = t.root;
		if (r.target === 'notes') return `Notes${r.detached ? ' · detached' : ''}: “${clip(r.quote, 90)}”`;
		if (r.detached) return 'No longer in the meeting';
		switch (r.target) {
			case 'decision':
				return `Decision: ${clip(blockText(t), 90)}`;
			case 'item':
				return `Action item: ${clip(blockText(t), 90)}`;
			case 'question':
				return `Question: ${clip(blockText(t), 90)}`;
			case 'body':
				return r.quote ? `Summary: “${clip(r.quote, 90)}”` : 'The summary';
			case 'segment':
				if (r.segment_idx != null) return `Transcript line ${r.segment_idx + 1}`;
				if (r.at_seconds != null) return `At ${fmtTime(r.at_seconds)}`;
				return 'Transcript';
			default:
				return 'The meeting';
		}
	}

	/** Where "Go to" leads, or null when there is nowhere to go. */
	function goTo(t: Thread): (() => void) | null {
		const r = t.root;
		if (r.detached) return null;
		if (r.target === 'notes') {
			// The Notes tab selects the marked text and opens the thread.
			return () => {
				uiState.focusNotesComment = r.id;
				uiState.activeTab = 'notes';
			};
		}
		if (r.target === 'decision' || r.target === 'item' || r.target === 'question') {
			if (!r.target_id || !commentsState.byBlock.has(r.target_id)) return null;
			return () => ongoto(r.target_id);
		}
		if (r.target === 'segment' && r.segment_idx != null) {
			return () => {
				uiState.focusComment = r.id; // the transcript scrolls to the line and opens its panel
				uiState.activeTab = 'transcript';
			};
		}
		const at = r.at_seconds;
		if (r.target === 'segment' && at != null && session) {
			// A moment between lines: the transcript where it falls, and the player there.
			const id = session.id;
			const transcript = session.transcript;
			return () => {
				const idx = transcript.findIndex((seg) => seg.start >= at - 0.01);
				sessionState.pendingOpen = { sessionId: id, idx: idx < 0 ? null : idx };
				if (playerState.available) playerState.seek(at, false);
			};
		}
		return null;
	}

	const FILTERS: [Filter, string][] = [
		['open', 'Open'],
		['resolved', 'Resolved'],
		['all', 'All']
	];
</script>

{#if session && (commentsState.threads.length || commentsState.canComment)}
	<section class="bg-gray-900 border border-gray-700 rounded-lg p-3 space-y-2" aria-label="Comments">
		<div class="flex flex-wrap items-center gap-2">
			<h4 class="text-xs font-semibold text-gray-400 uppercase tracking-wide">Comments</h4>
			<span class="text-[11px] text-gray-500 mr-auto">({commentsState.openCount} open)</span>
			<div class="flex rounded border border-gray-700 overflow-hidden text-[11px]" role="group" aria-label="Show comments">
				{#each FILTERS as [f, label] (f)}
					<button
						onclick={() => (filter = f)}
						aria-pressed={filter === f}
						class="px-2 py-0.5 {filter === f ? 'bg-gray-700 text-gray-100' : 'bg-gray-800 text-gray-400 hover:text-gray-200'}"
					>{label}</button>
				{/each}
			</div>
			{#if commentsState.canComment}
				<button onclick={() => (composing = !composing)} class="text-[11px] text-gray-400 hover:text-gray-200">Comment on the summary</button>
			{/if}
		</div>
		{#if composing && commentsState.canComment}
			<CommentComposer
				sessionId={session.id}
				target={{ target: 'body', target_id: '', exact: '', prefix: '', suffix: '', parent_id: '' }}
				placeholder="Comment on the summary"
				autofocus
				onposted={() => (composing = false)}
				oncancel={() => (composing = false)}
			/>
		{/if}
		{#if commentsState.error}<p class="text-xs text-red-400">{commentsState.error}</p>{/if}
		{#each shown as t (t.root.id)}
			{@const go = goTo(t)}
			<div class="space-y-1" data-comment-thread={t.root.id}>
				<div class="flex items-baseline gap-2 text-[11px] text-gray-500">
					<span class="min-w-0 truncate" title={onWhat(t)}>On: {onWhat(t)}</span>
					{#if go}<button onclick={go} class="shrink-0 text-blue-400 hover:text-blue-300">Go to</button>{/if}
				</div>
				<CommentThread thread={t} sessionId={session.id} />
			</div>
		{:else}
			<p class="text-[11px] text-gray-600">
				{commentsState.threads.length
					? filter === 'open'
						? 'No open comments.'
						: 'No resolved comments.'
					: 'No comments yet. Select text in the summary, or use Comment on a decision, action item or question.'}
			</p>
		{/each}
	</section>
{/if}
