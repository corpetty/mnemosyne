<script lang="ts">
	// A checklist of what to include (the publish dialog's Content, the people-share dialog's parts).
	let {
		items,
		sel,
		onchange,
		heading
	}: {
		items: { key: string; label: string; count?: number }[];
		sel: Record<string, boolean>;
		onchange: (key: string, value: boolean) => void;
		heading?: string;
	} = $props();

	const check = 'rounded border-gray-600 bg-gray-800';
</script>

<section aria-label={heading ?? 'Content'}>
	{#if heading}<h4 class="mb-1.5 text-[11px] uppercase tracking-wide text-gray-500">{heading}</h4>{/if}
	<div class="grid grid-cols-2 gap-x-4 gap-y-1">
		{#each items as c (c.key)}
			<label class="flex items-center gap-2 text-gray-300">
				<input type="checkbox" class={check} checked={!!sel[c.key]} onchange={(e) => onchange(c.key, e.currentTarget.checked)} />
				<span>{c.label}{#if c.count != null}<span class="text-gray-500">{` · ${c.count}`}</span>{/if}</span>
			</label>
		{/each}
	</div>
</section>
