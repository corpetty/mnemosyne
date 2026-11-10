<script lang="ts">
	// Settings → Devices and people → Your team: join a team that another computer hosts, and, once
	// joined, how team recordings are done by default, the meetings waiting to move to the team,
	// and leaving. Always about THIS computer (its own backend, api/local.ts), whichever library the
	// window shows. Backend services/team_member.py, services/team_move.py.
	import { getLocalSyncStatus, joinTeam, queueMove, setTeamWork, unqueueMove } from '$lib/api/local.js';
	import { askMoveConsent, leaveCurrentTeam } from '$lib/app/controller.svelte.js';
	import { moveNeedsConsent } from '$lib/app/teamDestination.js';
	import { connectionState } from '$lib/stores/connection.svelte.js';
	import { teamMemberState } from '$lib/stores/teamMember.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { PendingMove } from '$lib/types/index.js';

	const plain = (e: unknown, fallback: string) => (e instanceof Error ? e.message.replace(/^\d+: /, '') : fallback);

	let invite = $state('');
	let name = $state('');
	let busy = $state(false);
	let error = $state('');
	let workBusy = $state(false);
	let workError = $state('');
	let moveBusy = $state<string | null>(null);

	const joined = $derived(teamMemberState.joined);
	const team = $derived(teamMemberState.teamName);
	const user = $derived(teamMemberState.status?.user ?? null);
	// A computer that uses a main computer cannot also join a team (the backend answers 409 too).
	const usesMain = $derived(connectionState.usesMain);
	const pending = $derived(teamMemberState.moves?.pending ?? []);

	// This computer's name is the one it has for library sync, which the person already chose.
	$effect(() => {
		if (name) return;
		getLocalSyncStatus()
			.then((s) => {
				if (!name) name = s.self?.name ?? '';
			})
			.catch(() => {});
	});

	// The waiting meetings, now and whenever their number changes.
	$effect(() => {
		void teamMemberState.pending;
		if (joined) void teamMemberState.loadMoves();
	});

	async function join() {
		busy = true;
		error = '';
		try {
			teamMemberState.apply(await joinTeam(invite.trim(), name.trim()));
			invite = '';
			void teamMemberState.loadMoves();
		} catch (e) {
			error = plain(e, 'Could not join the team');
		} finally {
			busy = false;
		}
	}

	async function pickWork(work: 'here' | 'host') {
		if (work === teamMemberState.work) return;
		workBusy = true;
		workError = '';
		try {
			teamMemberState.apply(await setTeamWork(work));
		} catch (e) {
			workError = plain(e, 'Could not save that');
		} finally {
			workBusy = false;
		}
	}

	async function retry(p: PendingMove) {
		moveBusy = p.session_id;
		try {
			await queueMove(p.session_id);
			await teamMemberState.loadMoves();
		} catch (e) {
			toastState.error(plain(e, 'Could not try again'));
		} finally {
			moveBusy = null;
		}
	}

	async function remove(p: PendingMove) {
		moveBusy = p.session_id;
		try {
			await unqueueMove(p.session_id);
			await Promise.all([teamMemberState.load(), teamMemberState.loadMoves()]);
		} catch (e) {
			toastState.error(plain(e, 'Could not remove it from the queue'));
		} finally {
			moveBusy = null;
		}
	}

	async function leave() {
		if (
			!confirm(
				`Leave ${team}? Meetings already in the team stay there; recordings still waiting to move stay in your library.`
			)
		)
			return;
		busy = true;
		try {
			await leaveCurrentTeam();
		} finally {
			busy = false;
		}
	}

	const moveState = (p: PendingMove) =>
		p.host_session_id ? 'The team has it; the copy here is removed next' : p.last_error || 'Waiting to move';
</script>

<section id="your-team" aria-label="Your team">
	<h3 class="text-lg font-semibold text-gray-200 mb-1">Your team</h3>
	{#if !joined}
		<div class="rounded border border-gray-700 p-3 space-y-2 {usesMain ? 'opacity-60' : ''}" aria-label="Join a team">
			<h4 class="text-sm font-medium text-gray-300">Join a team</h4>
			{#if usesMain}
				<p class="text-xs text-gray-500">
					This computer uses a main computer. Disconnect it under This computer first: a computer cannot do both.
				</p>
			{/if}
			<div class="grid grid-cols-1 sm:grid-cols-[1fr_12rem] gap-3">
				<label>
					<span class="block text-xs text-gray-500 mb-1">Paste the app invite</span>
					<input
						type="text"
						bind:value={invite}
						disabled={usesMain || busy}
						placeholder="endpoint…#…"
						class="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 w-full font-mono disabled:opacity-50"
					/>
				</label>
				<label>
					<span class="block text-xs text-gray-500 mb-1">This computer's name</span>
					<input
						type="text"
						bind:value={name}
						disabled={usesMain || busy}
						class="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 w-full disabled:opacity-50"
					/>
				</label>
			</div>
			<div>
				<button
					onclick={join}
					disabled={usesMain || busy || !invite.trim()}
					class="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 disabled:opacity-50"
					>{busy ? 'Joining…' : 'Join'}</button
				>
				{#if error}<p class="mt-1.5 text-xs text-red-400">{error}</p>{/if}
			</div>
			<p class="text-xs text-gray-500">Your library stays on this computer. Only meetings you put in the team go to it.</p>
		</div>
	{:else}
		<div class="rounded border border-gray-700 p-3 space-y-4">
			<div class="space-y-0.5">
				<div class="flex items-center gap-2">
					<span class="text-sm font-medium text-gray-200">{team}</span>
					<span
						class="flex items-center gap-1.5 text-xs {teamMemberState.reachable ? 'text-gray-500' : 'text-amber-300'}"
						role="status"
					>
						<span class="w-2 h-2 rounded-full {teamMemberState.reachable ? 'bg-green-500' : 'bg-amber-400'}"></span>
						{teamMemberState.signedOut
							? "signed out"
							: teamMemberState.reachable
								? 'reachable'
								: "can't be reached"}
					</span>
				</div>
				{#if user}<p class="text-xs text-gray-500">You are {user.name}, {user.role}</p>{/if}
				{#if teamMemberState.signedOut}
					<p class="text-xs text-amber-300">You were signed out of {team}. Ask for a new invite.</p>
				{/if}
			</div>

			<fieldset class="space-y-1.5" disabled={workBusy}>
				<legend class="text-xs text-gray-500 mb-1">Team recordings are done</legend>
				<div class="flex flex-wrap gap-4" role="radiogroup" aria-label="Where team recordings are done">
					<label class="flex items-center gap-2 text-sm text-gray-300">
						<input
							type="radio"
							name="team-work"
							checked={teamMemberState.work === 'here'}
							onchange={() => pickWork('here')}
						/>
						On this computer
					</label>
					<label class="flex items-center gap-2 text-sm text-gray-300">
						<input
							type="radio"
							name="team-work"
							checked={teamMemberState.work === 'host'}
							onchange={() => pickWork('host')}
						/>
						On {team}'s computer
					</label>
				</div>
				{#if workError}<p class="text-xs text-red-400">{workError}</p>{/if}
				<p class="text-xs text-gray-500">You can choose again for each recording from the menu beside Record.</p>
			</fieldset>

			{#if pending.length}
				<div class="space-y-1.5">
					<h4 class="text-xs text-gray-500">Waiting to move to {team}</h4>
					<ul class="divide-y divide-gray-800" aria-label="Meetings waiting to move">
						{#each pending as p (p.session_id)}
							<li class="py-2 space-y-0.5">
								<p class="text-sm text-gray-200 truncate">{p.name || 'Untitled meeting'}</p>
								<p class="text-xs {moveNeedsConsent(p.last_error) ? 'text-amber-300' : 'text-gray-400'}">{moveState(p)}</p>
								<div class="flex items-center gap-3 pt-0.5">
									{#if moveNeedsConsent(p.last_error)}
										<button
											onclick={() => askMoveConsent(p.session_id)}
											disabled={moveBusy === p.session_id}
											class="rounded bg-gray-800 border border-gray-700 px-2 py-0.5 text-xs text-gray-200 hover:bg-gray-700 disabled:opacity-50"
											>Add how they agreed</button
										>
									{/if}
									<button
										onclick={() => retry(p)}
										disabled={moveBusy === p.session_id}
										class="rounded bg-gray-800 border border-gray-700 px-2 py-0.5 text-xs text-gray-200 hover:bg-gray-700 disabled:opacity-50"
										>Retry</button
									>
									<button
										onclick={() => remove(p)}
										disabled={moveBusy === p.session_id || !!p.host_session_id}
										class="text-xs text-gray-400 hover:text-gray-200 disabled:opacity-50">Remove from the queue</button
									>
								</div>
							</li>
						{/each}
					</ul>
				</div>
			{/if}

			<div>
				<button onclick={leave} disabled={busy} class="text-sm text-gray-400 hover:text-red-400 disabled:opacity-50"
					>Leave the team</button
				>
			</div>
		</div>
	{/if}
</section>
