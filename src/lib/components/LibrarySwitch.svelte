<script lang="ts">
	import { leaveCurrentTeam, openYourTeam, switchLibrary } from '$lib/app/controller.svelte.js';
	import { connectionState, SAME_ORIGIN } from '$lib/stores/connection.svelte.js';
	import { teamMemberState } from '$lib/stores/teamMember.svelte.js';

	// Above the meeting list when this computer has joined a team: which library the window shows,
	// yours or the team's, one at a time. The team's is reached through this computer; when its
	// computer cannot be reached, or signed this one out, the list says so here.
	const team = $derived(teamMemberState.teamName);
	const inTeam = $derived(connectionState.inTeam);
	let leaving = $state(false);

	async function leave() {
		if (
			!confirm(
				`Leave ${team}? Meetings already in the team stay there; recordings still waiting to move stay in your library.`
			)
		)
			return;
		leaving = true;
		try {
			await leaveCurrentTeam();
		} finally {
			leaving = false;
		}
	}
</script>

{#if teamMemberState.joined && !SAME_ORIGIN}
	<div class="space-y-2">
		<div class="flex rounded-lg bg-gray-900 border border-gray-800 p-0.5" role="group" aria-label="Library">
			<button
				onclick={() => switchLibrary('mine')}
				aria-pressed={!inTeam}
				class="min-w-0 flex-1 truncate px-2 py-1 rounded-md text-xs font-medium transition-colors
					{!inTeam ? 'bg-gray-700 text-gray-100' : 'text-gray-400 hover:text-gray-200'}"
			>
				Your library
			</button>
			<button
				onclick={() => switchLibrary('team')}
				aria-pressed={inTeam}
				title={team}
				class="min-w-0 flex-1 truncate px-2 py-1 rounded-md text-xs font-medium transition-colors
					{inTeam ? 'bg-gray-700 text-gray-100' : 'text-gray-400 hover:text-gray-200'}"
			>
				{team}
			</button>
		</div>
		{#if inTeam && teamMemberState.signedOut}
			<div class="rounded border border-amber-900 bg-amber-950/30 px-2 py-1.5 text-xs text-amber-200 space-y-1" role="status">
				<p>You were signed out of {team}. Ask for a new invite.</p>
				<button onclick={leave} disabled={leaving} class="underline hover:text-amber-100 disabled:opacity-50">Leave</button>
			</div>
		{:else if inTeam && !teamMemberState.reachable}
			<div class="rounded border border-amber-900 bg-amber-950/30 px-2 py-1.5 text-xs text-amber-200 space-y-1" role="status">
				<p>
					{team}'s computer can't be reached. Your library still works. If you were removed from the team, ask for a new
					invite.
				</p>
				<button onclick={() => switchLibrary('mine')} class="underline hover:text-amber-100">Back to your library</button>
			</div>
		{/if}
	</div>
{/if}
