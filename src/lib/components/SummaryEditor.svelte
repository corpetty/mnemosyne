<script lang="ts">
	// Edit a summary by hand (backend services/summary_edit.py): the text, topics (tags),
	// decisions, action items, chapters, open questions and client facts, saved together.
	// Each decision and question keeps its transcript time, so its link survives edits.
	import { untrack } from 'svelte';
	import { editSummary } from '$lib/api/backend.js';
	import { FACT_HEADINGS } from '$lib/app/facts.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { ActionItem, ClientFact, SessionDetail, TimedText } from '$lib/types/index.js';

	let { session, ondone }: { session: SessionDetail; ondone: () => void } = $props();

	type ChapterRow = { time: string; title: string };

	function fmt(sec: number): string {
		const s = Math.max(0, Math.round(sec));
		const h = Math.floor(s / 3600);
		const m = Math.floor((s % 3600) / 60);
		const r = String(s % 60).padStart(2, '0');
		return h ? `${h}:${String(m).padStart(2, '0')}:${r}` : `${m}:${r}`;
	}

	/** "1:02:03", "12:30" or "90" → seconds; null when it is not a time. */
	function parse(time: string): number | null {
		const parts = time.trim().split(':');
		if (!parts.length || parts.length > 3 || parts.some((p) => !/^\d+(\.\d+)?$/.test(p))) return null;
		return parts.reduce((total, p) => total * 60 + Number(p), 0);
	}

	// The editor opens on a copy of the summary as it is now; later changes do not move it.
	const start = untrack(() => session);
	const d = start.summary_data;
	const timed = (texts: string[], at: (number | null)[]): TimedText[] =>
		texts.map((text, i) => ({ text, at: at[i] ?? null }));

	let summary = $state(start.summary ?? '');
	let topics = $state<string[]>([...(d?.topics ?? [])]);
	let newTopic = $state('');
	let decisions = $state<TimedText[]>(timed(d?.decisions ?? [], d?.decision_at ?? []));
	let questions = $state<TimedText[]>(timed(d?.open_questions ?? [], d?.question_at ?? []));
	let items = $state<ActionItem[]>((d?.action_items ?? []).map((a) => ({ ...a })));
	let chapters = $state<ChapterRow[]>((d?.chapters ?? []).map((c) => ({ time: fmt(c.start), title: c.title })));
	let facts = $state<ClientFact[]>((d?.client_facts ?? []).map((f) => ({ ...f })));
	const showFacts = d?.style === 'client' || (d?.client_facts.length ?? 0) > 0;
	let saving = $state(false);

	const badTime = $derived(chapters.some((c) => c.title.trim() && parse(c.time) === null));

	function addTopic() {
		const t = newTopic.replace(/,/g, ' ').trim();
		if (t && !topics.some((x) => x.toLowerCase() === t.toLowerCase())) topics = [...topics, t];
		newTopic = '';
	}

	function topicKey(e: KeyboardEvent) {
		if (e.key === 'Enter' || e.key === ',') {
			e.preventDefault();
			addTopic();
		} else if (e.key === 'Backspace' && !newTopic && topics.length) {
			topics = topics.slice(0, -1);
		}
	}

	function move<T>(list: T[], i: number, by: number): T[] {
		const j = i + by;
		if (j < 0 || j >= list.length) return list;
		const next = [...list];
		[next[i], next[j]] = [next[j], next[i]];
		return next;
	}

	async function save() {
		if (saving || badTime) return;
		saving = true;
		try {
			addTopic();
			await editSummary(start.id, {
				summary,
				topics,
				decisions,
				open_questions: questions,
				action_items: items.map((a) => ({ ...a, due: a.due || null, owner: a.owner || null })),
				chapters: chapters
					.filter((c) => c.title.trim())
					.map((c) => ({ start: parse(c.time) ?? 0, title: c.title })),
				client_facts: facts
			});
			await sessionState.refreshActive();
			toastState.success('Summary saved');
			ondone();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not save the summary');
		} finally {
			saving = false;
		}
	}

	function keydown(e: KeyboardEvent) {
		if (e.key === 'Escape') ondone();
		else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) save();
	}

	const input = 'bg-gray-950 border border-gray-700 rounded px-2 py-1 text-sm text-gray-200 min-w-0';
	const small = 'text-xs text-gray-500 hover:text-gray-200 px-1';
	const add = 'text-xs text-blue-400 hover:text-blue-300';
	const heading = 'text-xs font-semibold text-gray-400 uppercase tracking-wide mb-2';
</script>

{#snippet when(at: number | null | undefined)}
	{#if at != null}<span class="font-mono text-[11px] text-gray-600 w-10 shrink-0 text-right" title="Where it came up in the transcript">{fmt(at)}</span>{/if}
{/snippet}

{#snippet rows(list: TimedText[], set: (next: TimedText[]) => void, label: string)}
	<ul class="space-y-1.5">
		{#each list as row, i}
			<li class="flex items-center gap-1.5">
				<input bind:value={row.text} aria-label="{label} {i + 1}" class="{input} flex-1" />
				{@render when(row.at)}
				<button onclick={() => set(move(list, i, -1))} disabled={i === 0} class={small} aria-label="Move {label.toLowerCase()} {i + 1} up">↑</button>
				<button onclick={() => set(list.filter((_, j) => j !== i))} class={small} aria-label="Remove {label.toLowerCase()} {i + 1}">✕</button>
			</li>
		{/each}
	</ul>
	<button onclick={() => set([...list, { text: '', at: null }])} class="{add} mt-1.5">+ Add {label.toLowerCase()}</button>
{/snippet}

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="space-y-3" onkeydown={keydown} aria-label="Edit summary">
	<section class="bg-gray-900 border border-gray-700 rounded-lg p-3">
		<h4 class={heading}>Topics</h4>
		<div class="flex flex-wrap items-center gap-1">
			{#each topics as t, i}
				<span class="text-[11px] pl-2 pr-1 py-0.5 rounded-full bg-gray-800 text-gray-300 border border-gray-700 flex items-center gap-1">
					{t}
					<button onclick={() => (topics = topics.filter((_, j) => j !== i))} class="text-gray-500 hover:text-gray-200" aria-label="Remove topic {t}">✕</button>
				</span>
			{/each}
			<input
				bind:value={newTopic}
				onkeydown={topicKey}
				onblur={addTopic}
				placeholder="Add a topic…"
				aria-label="Add a topic"
				class="bg-transparent text-xs text-gray-200 px-1 py-0.5 outline-none min-w-[8rem] flex-1"
			/>
		</div>
	</section>

	<section class="bg-gray-900 border border-gray-700 rounded-lg p-3">
		<h4 class={heading}>Summary</h4>
		<textarea
			bind:value={summary}
			rows={Math.min(24, Math.max(6, summary.split('\n').length + 1))}
			aria-label="Summary text"
			class="w-full {input} font-sans resize-y"
		></textarea>
		<p class="text-[11px] text-gray-600 mt-1">Markdown: **bold**, - lists, ## headings.</p>
	</section>

	<div class="grid gap-3 md:grid-cols-2">
		<section class="bg-gray-900 border border-gray-700 rounded-lg p-3">
			<h4 class={heading}>Decisions</h4>
			{@render rows(decisions, (next) => (decisions = next), 'Decision')}
		</section>

		<section class="bg-gray-900 border border-gray-700 rounded-lg p-3">
			<h4 class={heading}>Action items</h4>
			<ul class="space-y-2">
				{#each items as a, i}
					<li class="space-y-1">
						<div class="flex items-center gap-1.5">
							<input type="checkbox" bind:checked={a.done} class="rounded border-gray-600 bg-gray-800" aria-label="Done: action item {i + 1}" />
							<input bind:value={a.text} aria-label="Action item {i + 1}" class="{input} flex-1" />
							{@render when(a.at)}
							<button onclick={() => (items = move(items, i, -1))} disabled={i === 0} class={small} aria-label="Move action item {i + 1} up">↑</button>
							<button onclick={() => (items = items.filter((_, j) => j !== i))} class={small} aria-label="Remove action item {i + 1}">✕</button>
						</div>
						<div class="flex items-center gap-1.5 pl-6">
							<input
								value={a.owner ?? ''}
								oninput={(e) => (a.owner = e.currentTarget.value)}
								placeholder="Owner"
								aria-label="Owner of action item {i + 1}"
								class="{input} w-40"
							/>
							<input
								type="date"
								value={a.due ?? ''}
								oninput={(e) => (a.due = e.currentTarget.value || null)}
								aria-label="Due date of action item {i + 1}"
								class="{input} w-40"
							/>
							{#if a.issue_url}<span class="text-[11px] text-green-500" title={a.issue_url}>has an issue</span>{/if}
						</div>
					</li>
				{/each}
			</ul>
			<button onclick={() => (items = [...items, { text: '', owner: null, issue_url: null, done: false, live: false, at: null, due: null }])} class="{add} mt-1.5">+ Add action item</button>
		</section>

		<section class="bg-gray-900 border border-gray-700 rounded-lg p-3 md:col-span-2">
			<h4 class={heading}>Chapters</h4>
			<ul class="space-y-1.5">
				{#each chapters as c, i}
					<li class="flex items-center gap-1.5">
						<input
							bind:value={c.time}
							aria-label="Start of chapter {i + 1}"
							aria-invalid={parse(c.time) === null}
							class="{input} w-20 font-mono text-xs {parse(c.time) === null ? 'border-red-500' : ''}"
						/>
						<input bind:value={c.title} aria-label="Chapter {i + 1}" class="{input} flex-1" />
						<button onclick={() => (chapters = chapters.filter((_, j) => j !== i))} class={small} aria-label="Remove chapter {i + 1}">✕</button>
					</li>
				{/each}
			</ul>
			<button onclick={() => (chapters = [...chapters, { time: chapters.length ? chapters[chapters.length - 1].time : '0:00', title: '' }])} class="{add} mt-1.5">+ Add chapter</button>
			{#if badTime}<p class="text-[11px] text-red-400 mt-1">A start time is not a time: use 12:30 or 1:02:03.</p>{/if}
		</section>

		<section class="bg-gray-900 border border-gray-700 rounded-lg p-3 md:col-span-2">
			<h4 class={heading}>Open questions</h4>
			{@render rows(questions, (next) => (questions = next), 'Question')}
		</section>

		{#if showFacts}
			<section class="bg-gray-900 border border-gray-700 rounded-lg p-3 md:col-span-2" aria-label="Client facts">
				<h4 class={heading}>Client facts</h4>
				<ul class="space-y-1.5">
					{#each facts as f, i}
						<li class="flex items-center gap-1.5">
							<select bind:value={f.kind} aria-label="Kind of client fact {i + 1}" class="{input} w-36">
								{#each FACT_HEADINGS as [kind, label]}<option value={kind}>{label}</option>{/each}
							</select>
							<input bind:value={f.text} aria-label="Client fact {i + 1}" class="{input} flex-1" />
							{@render when(f.at)}
							<button onclick={() => (facts = facts.filter((_, j) => j !== i))} class={small} aria-label="Remove client fact {i + 1}">✕</button>
						</li>
					{/each}
				</ul>
				<button onclick={() => (facts = [...facts, { kind: 'other', text: '', at: null }])} class="{add} mt-1.5">+ Add client fact</button>
			</section>
		{/if}
	</div>

	<div class="flex items-center gap-2">
		<button
			onclick={save}
			disabled={saving || badTime}
			class="px-4 py-1.5 text-sm rounded bg-blue-600 hover:bg-blue-700 disabled:bg-gray-700 disabled:text-gray-500 text-white font-medium"
		>{saving ? 'Saving…' : 'Save summary'}</button>
		<button onclick={ondone} class="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300">Cancel</button>
		<span class="text-[11px] text-gray-600">Ctrl+Enter saves · Esc cancels</span>
	</div>
</div>
