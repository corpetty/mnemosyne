<script lang="ts">
	import { setAgenda } from '$lib/api/backend.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { AgendaItem } from '$lib/types/index.js';

	// The points to get through. Before the meeting: write them (or they come from the calendar
	// event). During it: the copilot ticks the ones that came up (`covered`, 1-based, live).
	let { covered = [], compact = false }: { covered?: number[]; compact?: boolean } = $props();

	const session = $derived(sessionState.activeSession);
	const items = $derived(session?.agenda ?? []);
	const done = (i: number) => items[i].covered || covered.includes(i + 1);
	const count = $derived(items.filter((_, i) => done(i)).length);
	let draft = $state('');
	let busy = $state(false);

	async function save(next: AgendaItem[]) {
		if (!session) return;
		busy = true;
		try {
			await setAgenda(session.id, next);
			await sessionState.refreshActive();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not save the agenda');
		} finally {
			busy = false;
		}
	}

	function add() {
		const text = draft.trim();
		if (!text) return;
		draft = '';
		save([...items, { text, covered: false }]);
	}
</script>

{#if session && (items.length || !compact)}
	<section class={compact ? 'space-y-1.5' : 'rounded-lg border border-gray-800 bg-gray-900/40 p-3 space-y-2'} aria-label="Agenda">
		<header class="flex items-baseline gap-2">
			<h4 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500">Agenda</h4>
			{#if items.length}<span class="text-xs text-gray-500">{count} of {items.length} covered</span>{/if}
		</header>
		{#if items.length}
			<ul class="space-y-1 text-sm">
				{#each items as item, i (i)}
					<li class="group flex items-start gap-2">
						<input
							type="checkbox"
							checked={done(i)}
							disabled={busy}
							onchange={() => save(items.map((a, j) => (j === i ? { ...a, covered: !done(i) } : a)))}
							aria-label={`Covered: ${item.text}`}
							class="mt-1 rounded border-gray-600 bg-gray-800"
						/>
						<span class={done(i) ? 'text-gray-500 line-through' : 'text-gray-200'}>{item.text}</span>
						<button
							onclick={() => save(items.filter((_, j) => j !== i))}
							disabled={busy}
							class="ml-auto opacity-0 group-hover:opacity-100 px-1 text-gray-500 hover:text-gray-200"
							aria-label={`Remove ${item.text}`}
						>✕</button>
					</li>
				{/each}
			</ul>
		{:else}
			<p class="text-xs text-gray-500">What should this meeting get through? The copilot ticks points off as they come up.</p>
		{/if}
		<input
			bind:value={draft}
			onkeydown={(e) => e.key === 'Enter' && add()}
			disabled={busy}
			placeholder="Add a point, then Enter"
			class="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-200"
		/>
	</section>
{/if}
