<script lang="ts">
	// The meeting's agenda beside the notes (from the sealed meta): covered items are ticked as
	// the owner's copilot gets through them. No agenda, nothing shown.
	import type { AgendaItem } from './doc';

	let { items }: { items: readonly AgendaItem[] } = $props();
</script>

{#if items.length}
	<section aria-labelledby="guest-agenda-heading" data-testid="guest-agenda">
		<h2 id="guest-agenda-heading" class="text-sm font-semibold text-gray-300">Agenda</h2>
		<ul class="mt-2 space-y-1.5">
			{#each items as item, i (i)}
				<li class="flex items-start gap-2 text-sm {item.covered ? 'text-gray-500' : 'text-gray-200'}">
					{#if item.covered}
						<span class="mt-0.5 w-4 shrink-0 text-center text-green-400" aria-hidden="true">✓</span>
						<span class="sr-only">Covered: </span>
					{:else}
						<span
							class="mx-px mt-1 h-3.5 w-3.5 shrink-0 rounded-full border border-gray-500"
							aria-hidden="true"
						></span>
					{/if}
					<span class="min-w-0 break-words">{item.text}</span>
				</li>
			{/each}
		</ul>
	</section>
{/if}
