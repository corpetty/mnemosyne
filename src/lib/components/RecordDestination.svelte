<script lang="ts">
	import type { Destination } from '$lib/app/teamDestination.js';
	import { teamMemberState } from '$lib/stores/teamMember.svelte.js';

	// Beside Record when this computer has joined a team: where this recording goes. The choice
	// lasts for that recording only; without one the default is your library while you look at it,
	// and the work default from Settings → Your team while you look at the team's library.
	let { disabled = false }: { disabled?: boolean } = $props();

	let open = $state(false);
	let root = $state<HTMLElement>();

	const team = $derived(teamMemberState.teamName);
	const options = $derived<{ value: Destination; label: string; hint: string }[]>([
		{ value: 'mine', label: 'Your library', hint: '' },
		{
			value: 'team-here',
			label: `${team}, done on this computer`,
			hint: 'Moves to the team when transcribed'
		},
		{
			value: 'team-host',
			label: `${team}, done on ${team}'s computer`,
			hint: 'Streams as you record; needs a connection'
		}
	]);
	const current = $derived(teamMemberState.destination);

	function choose(d: Destination) {
		teamMemberState.choose(d);
		open = false;
	}
</script>

<svelte:window
	onclick={(e) => {
		if (open && root && !root.contains(e.target as Node)) open = false;
	}}
	onkeydown={(e) => {
		if (open && e.key === 'Escape') open = false;
	}}
/>

<span class="relative" bind:this={root}>
	<button
		onclick={() => (open = !open)}
		{disabled}
		aria-label="Where this recording goes"
		aria-haspopup="menu"
		aria-expanded={open}
		title={options.find((o) => o.value === current)?.label}
		class="flex items-center px-2 py-2 rounded-lg bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
	>
		<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 9l6 6 6-6" />
		</svg>
	</button>
	{#if open}
		<div
			class="absolute left-0 z-30 mt-1 w-72 rounded-lg border border-gray-700 bg-gray-900 py-1 text-sm shadow-xl"
			role="menu"
			aria-label="Where this recording goes"
		>
			{#each options as o (o.value)}
				<button
					role="menuitemradio"
					aria-checked={current === o.value}
					onclick={() => choose(o.value)}
					class="flex w-full items-start gap-2 px-3 py-1.5 text-left text-gray-200 hover:bg-gray-800"
				>
					<span class="mt-0.5 w-4 shrink-0 text-blue-400">
						{#if current === o.value}
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
							</svg>
						{/if}
					</span>
					<span class="min-w-0">
						<span class="block">{o.label}</span>
						{#if o.hint}<span class="block text-xs text-gray-500">{o.hint}</span>{/if}
					</span>
				</button>
			{/each}
		</div>
	{/if}
</span>
