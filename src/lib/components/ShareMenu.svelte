<script lang="ts">
	import { getShares, setShares } from '$lib/api/backend.js';
	import { connectionState } from '$lib/stores/connection.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { teamState } from '$lib/stores/team.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { Shares } from '$lib/types/index.js';

	// Who may read this meeting besides its owner, on a team server (backend services/sharing.py).
	// The owner and admins change it; everyone else sees who it is shared with.
	let shares = $state<Shares | null>(null);
	let open = $state(false);
	let team = $state(false);
	let chosen = $state<Set<string>>(new Set());
	let saving = $state(false);
	let root = $state<HTMLElement | null>(null);
	const session = $derived(sessionState.activeSession);
	const others = $derived(teamState.people.filter((p) => p.id !== session?.owner_id && !p.disabled));

	$effect(() => {
		const id = session?.id;
		shares = null;
		if (!id) return;
		getShares(id)
			.then((s) => {
				if (id === sessionState.activeSession?.id) shares = s;
			})
			.catch(() => (shares = null));
	});

	function toggleOpen() {
		if (!shares) return;
		open = !open;
		team = shares.team;
		chosen = new Set(shares.people.map((p) => p.id));
	}

	function pick(id: string) {
		const next = new Set(chosen);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		chosen = next;
	}

	async function save() {
		if (!session) return;
		saving = true;
		try {
			shares = await setShares(session.id, team, [...chosen]);
			open = false;
			toastState.success(shares.team ? 'Shared with everyone' : shares.people.length ? 'Sharing saved' : 'Not shared');
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not share');
		} finally {
			saving = false;
		}
	}

	const label = $derived(
		!shares ? 'Share' : shares.team ? 'Shared with everyone' : shares.people.length ? `Shared · ${shares.people.length}` : 'Share'
	);
</script>

<svelte:window
	onclick={(e) => {
		if (open && root && !root.contains(e.target as Node)) open = false;
	}}
	onkeydown={(e) => {
		if (open && e.key === 'Escape') open = false;
	}}
/>

{#if connectionState.me?.team_mode && shares && (shares.can_change || shares.team || shares.people.length)}
	<span class="relative" bind:this={root}>
		<button
			onclick={toggleOpen}
			aria-expanded={open}
			title="Who can read this meeting"
			class="px-2 py-0.5 rounded border border-gray-800 {shares.team || shares.people.length ? 'text-blue-300' : 'text-gray-400'} hover:text-gray-200"
		>{label}</button>
		{#if open}
			<div class="absolute right-0 z-30 mt-1 w-64 rounded-lg border border-gray-700 bg-gray-900 p-3 text-sm shadow-xl space-y-2" role="dialog" aria-label="Share this meeting">
				{#if shares.can_change}
					<p class="text-xs text-gray-500">They can read it, listen to it and tick off its action items; only you change it.</p>
					<label class="flex items-center gap-2">
						<input type="checkbox" bind:checked={team} class="rounded border-gray-600 bg-gray-800" />
						<span class="text-gray-200">Everyone on the team</span>
					</label>
					{#if !team}
						<ul class="max-h-48 overflow-y-auto space-y-1">
							{#each others as p (p.id)}
								<li>
									<label class="flex items-center gap-2">
										<input type="checkbox" checked={chosen.has(p.id)} onchange={() => pick(p.id)} class="rounded border-gray-600 bg-gray-800" />
										<span class="text-gray-300">{p.name}</span>
									</label>
								</li>
							{:else}
								<li class="text-xs text-gray-500">Nobody else is on this server yet.</li>
							{/each}
						</ul>
					{/if}
					<button onclick={save} disabled={saving} class="w-full rounded bg-blue-600 px-3 py-1 text-white hover:bg-blue-500 disabled:opacity-50">
						{saving ? 'Saving…' : 'Save'}
					</button>
				{:else}
					<p class="text-gray-300">
						Shared with {shares.team ? 'everyone on the team' : shares.people.map((p) => p.name).join(', ')}.
					</p>
				{/if}
			</div>
		{/if}
	</span>
{/if}
