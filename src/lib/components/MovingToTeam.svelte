<script lang="ts">
	import { openYourTeam } from '$lib/app/controller.svelte.js';
	import { connectionState, SAME_ORIGIN } from '$lib/stores/connection.svelte.js';
	import { teamMemberState } from '$lib/stores/teamMember.svelte.js';

	// The bottom of "Your library": meetings recorded here that are waiting to move to the team.
	// They are still in this list until the team has them; the list of them is in Settings.
	const show = $derived(
		!SAME_ORIGIN && teamMemberState.joined && !connectionState.inTeam && teamMemberState.pending > 0
	);
</script>

{#if show}
	<button
		onclick={openYourTeam}
		class="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-xs text-gray-500 hover:bg-gray-800/50 hover:text-gray-300"
		title="Meetings waiting to move to {teamMemberState.teamName}"
	>
		<svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16a4 4 0 0 1-.9-7.9A5 5 0 0 1 15.9 6 4.5 4.5 0 0 1 17 15M12 12v8m0-8-3 3m3-3 3 3" />
		</svg>
		<span class="truncate">Moving to {teamMemberState.teamName} · {teamMemberState.pending}</span>
	</button>
{/if}
