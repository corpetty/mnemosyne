<script lang="ts">
	// Below `lg`, when readers may comment: "Meeting" | "Comments (n)", the guest page's segmented
	// control (guest/src/Tabs.svelte). The panels are Viewer.svelte's, linked by id.
	type ViewerTab = 'meeting' | 'comments';

	let { selected = $bindable('meeting'), comments = 0 }: { selected?: ViewerTab; comments?: number } = $props();

	const TABS: ViewerTab[] = ['meeting', 'comments'];
	const label = (t: ViewerTab) => (t === 'meeting' ? 'Meeting' : comments > 0 ? `Comments (${comments})` : 'Comments');

	function keydown(e: KeyboardEvent) {
		const i = TABS.indexOf(selected);
		const next =
			e.key === 'ArrowRight' || e.key === 'End'
				? TABS[e.key === 'End' ? TABS.length - 1 : (i + 1) % TABS.length]
				: e.key === 'ArrowLeft' || e.key === 'Home'
					? TABS[e.key === 'Home' ? 0 : (i + TABS.length - 1) % TABS.length]
					: null;
		if (!next) return;
		e.preventDefault();
		selected = next;
		document.getElementById(`viewer-tab-${next}`)?.focus();
	}
</script>

<div
	role="tablist"
	aria-label="Meeting and comments"
	tabindex="-1"
	onkeydown={keydown}
	data-testid="viewer-tabs"
	class="flex w-full gap-1 rounded-lg border border-gray-800 bg-gray-900 p-1"
>
	{#each TABS as t (t)}
		<button
			type="button"
			role="tab"
			id="viewer-tab-{t}"
			aria-selected={selected === t}
			aria-controls="viewer-panel-{t}"
			tabindex={selected === t ? 0 : -1}
			onclick={() => (selected = t)}
			class="min-h-10 flex-1 rounded-md border px-2 text-sm {selected === t
				? 'border-gray-600 bg-gray-800 font-medium text-gray-100'
				: 'border-transparent text-gray-400'}">{label(t)}</button
		>
	{/each}
</div>
