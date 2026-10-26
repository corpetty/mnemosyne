<script lang="ts">
	// The phone layout's segmented control: Notes, Agenda, Comments (with the count of open
	// threads). The panels are App.svelte's, linked by id (layout.ts tabId/panelId).
	import { panelId, tabForKey, tabId, tabsFor, type TabId } from './layout';

	let { selected = $bindable('notes'), comments = 0 }: { selected?: TabId; comments?: number } = $props();

	const tabs = $derived(tabsFor(comments));

	function keydown(e: KeyboardEvent) {
		const next = tabForKey(selected, e.key);
		if (!next) return;
		e.preventDefault();
		selected = next;
		document.getElementById(tabId(next))?.focus();
	}
</script>

<div
	role="tablist"
	aria-label="Notes, agenda and comments"
	tabindex="-1"
	onkeydown={keydown}
	data-testid="guest-tabs"
	class="flex w-full gap-1 rounded-lg border border-gray-800 bg-gray-900 p-1"
>
	{#each tabs as t (t.id)}
		<button
			type="button"
			role="tab"
			id={tabId(t.id)}
			aria-selected={selected === t.id}
			aria-controls={panelId(t.id)}
			tabindex={selected === t.id ? 0 : -1}
			onclick={() => (selected = t.id)}
			class="min-h-10 flex-1 rounded-md border px-2 text-sm {selected === t.id
				? 'border-gray-600 bg-gray-800 font-medium text-gray-100'
				: 'border-transparent text-gray-400'}"
		>
			{t.label}{#if t.count}{" "}<span class="text-xs text-gray-400" data-testid="guest-tab-count">{t.count}</span>{/if}
		</button>
	{/each}
</div>
