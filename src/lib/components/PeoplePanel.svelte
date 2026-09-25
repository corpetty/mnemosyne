<script lang="ts">
	import { uiState } from '$lib/stores/ui.svelte.js';
	import { getPerson, listPeople, setActionItemDone } from '$lib/api/backend.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { PersonDetail, PersonSummary, TaskItem } from '$lib/types/index.js';

	let { onOpenSession }: { onOpenSession?: () => void } = $props();

	let people = $state<PersonSummary[]>([]);
	let loaded = $state(false);
	let filter = $state('');
	let selected = $state<string | null>(null);
	let detail = $state<PersonDetail | null>(null);

	$effect(() => {
		listPeople()
			.then((p) => {
				people = p;
				loaded = true;
				if (!selected && p.length) selected = uiState.personRequest ?? p[0].name;
			})
			.catch((e) => toastState.error(e instanceof Error ? e.message : 'Could not load people'));
	});

	// The command palette asks for a person.
	$effect(() => {
		const want = uiState.personRequest;
		if (!want) return;
		selected = want;
		filter = '';
		uiState.personRequest = null;
	});

	$effect(() => {
		const name = selected;
		if (!name) return;
		getPerson(name)
			.then((d) => {
				if (selected === name) detail = d;
			})
			.catch(() => (detail = null));
	});

	function day(iso: string | null): string {
		return iso ? new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }) : '—';
	}

	function dur(sec: number): string {
		const m = Math.floor(sec / 60);
		return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}:${String(Math.round(sec % 60)).padStart(2, '0')}`;
	}

	async function open(id: string) {
		onOpenSession?.();
		await sessionState.selectSession(id);
	}

	async function toggle(t: TaskItem) {
		try {
			await setActionItemDone(t.session_id, t.idx, !t.done);
			if (selected) detail = await getPerson(selected);
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not update the task');
		}
	}

	const shown = $derived(people.filter((p) => p.name.toLowerCase().includes(filter.trim().toLowerCase())));
</script>

<div class="space-y-4">
	<div>
		<h2 class="text-xl font-semibold text-gray-100 mb-1">People</h2>
		<p class="text-xs text-gray-500">
			Everyone named in your meetings: speakers you have named, invitees from the calendar, action item owners and saved
			voices.
		</p>
	</div>

	{#if loaded && people.length === 0}
		<p class="text-sm text-gray-600">Nobody yet. Name speakers in a transcript, or connect a calendar, and people show up here.</p>
	{:else}
		<div class="grid gap-4 md:grid-cols-[14rem_1fr]">
			<div class="space-y-2">
				<input
					type="search"
					bind:value={filter}
					placeholder="Filter…"
					class="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200 placeholder-gray-600"
				/>
				<ul class="space-y-0.5 max-h-[60vh] overflow-y-auto">
					{#each shown as p (p.name)}
						<li>
							<button
								onclick={() => (selected = p.name)}
								class="w-full text-left rounded px-2 py-1 text-sm {selected === p.name ? 'bg-gray-800 text-gray-100' : 'text-gray-300 hover:bg-gray-900'}"
							>
								<span class="font-medium">{p.name}</span>{#if p.has_voice}<span class="ml-1 text-gray-600" title="Saved voice">●</span>{/if}
								<span class="block text-[11px] text-gray-500">
									{p.meetings} meeting{p.meetings === 1 ? '' : 's'}{#if p.open_tasks} · {p.open_tasks} open{/if} · {day(p.last_seen)}
								</span>
							</button>
						</li>
					{/each}
				</ul>
			</div>

			{#if detail}
				<article class="space-y-4" aria-label="Person {detail.name}">
					<header>
						<h3 class="text-lg font-semibold text-gray-100">{detail.name}</h3>
						<p class="text-xs text-gray-500">
							{detail.meetings.length} meetings{#if detail.total_talk_seconds} · talked {dur(detail.total_talk_seconds)} in total{/if}
							{#if detail.has_voice} · voice saved{/if}
						</p>
					</header>

					{#if detail.open_tasks.length}
						<section>
							<h4 class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-1">Open tasks</h4>
							<ul class="space-y-0.5 text-sm">
								{#each detail.open_tasks as t (t.session_id + t.idx)}
									<li class="flex gap-2 items-start">
										<input type="checkbox" onchange={() => toggle(t)} aria-label="Done: {t.text}" class="mt-1 rounded border-gray-600 bg-gray-800" />
										<span class="text-gray-200">{t.text}</span>
										<button onclick={() => open(t.session_id)} class="text-xs text-gray-500 hover:text-gray-300 pt-0.5 whitespace-nowrap">· {t.session_name}</button>
									</li>
								{/each}
							</ul>
						</section>
					{/if}

					<section>
						<h4 class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-1">Meetings</h4>
						<ul class="space-y-0.5 text-sm">
							{#each detail.meetings as m (m.id)}
								<li class="flex gap-3 items-baseline">
									<span class="text-xs text-gray-500 w-24 flex-shrink-0">{day(m.created_at)}</span>
									<button onclick={() => open(m.id)} class="text-left text-gray-200 hover:text-white">{m.name}</button>
									<span class="text-xs text-gray-500">
										{#if m.talk_seconds}spoke {dur(m.talk_seconds)} ({Math.round(100 * (m.share ?? 0))}%){:else}{m.role === 'invited' ? 'invited' : 'spoke'}{/if}
									</span>
								</li>
							{/each}
						</ul>
					</section>

					{#if detail.decisions.length}
						<section>
							<h4 class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-1">Decisions in their meetings</h4>
							<ul class="space-y-0.5 text-sm text-gray-300 list-disc list-inside">
								{#each detail.decisions.slice(0, 12) as d}
									<li>{d.text} <span class="text-xs text-gray-600">· {d.session_name}</span></li>
								{/each}
							</ul>
						</section>
					{/if}

					{#if detail.done_tasks.length}
						<details class="text-sm">
							<summary class="cursor-pointer text-xs text-gray-500 hover:text-gray-300">{detail.done_tasks.length} done</summary>
							<ul class="mt-1 space-y-0.5">
								{#each detail.done_tasks as t (t.session_id + t.idx)}
									<li class="text-gray-500 line-through">{t.text}</li>
								{/each}
							</ul>
						</details>
					{/if}
				</article>
			{/if}
		</div>
	{/if}
</div>
