<script lang="ts">
	import { onMount } from 'svelte';
	import { addUser, changeUser, inviteUser, listUsers, signOutUser } from '$lib/api/backend.js';
	import { copyText } from '$lib/app/clipboard.js';
	import { connectionState, SAME_ORIGIN } from '$lib/stores/connection.svelte.js';
	import { teamState } from '$lib/stores/team.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { UserInfo, UserInvite } from '$lib/types/index.js';

	// People on a team server (backend services/users.py): an admin adds them and sends each
	// an invite link, which signs one browser in.
	let people = $state<UserInfo[]>([]);
	let name = $state('');
	let email = $state('');
	let role = $state('member');
	let busy = $state(false);
	let invite = $state<{
		name: string;
		link: string;
		app: string | null;
		problem: string;
		until: string;
	} | null>(null);

	const ROLES: [string, string][] = [
		['member', 'Member: their own meetings'],
		['reviewer', 'Reviewer: reads every meeting'],
		['admin', 'Admin: everything, people and settings']
	];
	// A main computer that hosts a team: its owner is the only admin, and reviewers read the team's
	// meetings, not the owner's own (docs/plans/2026-11-09-team-hosting.md).
	const hosting = $derived(!!connectionState.me?.hosts_team);
	const HOST_ROLES: [string, string][] = [
		['member', 'Member: their own meetings'],
		['reviewer', "Reviewer: reads the team's meetings"]
	];
	const rolesFor = (current: string) => (hosting && current !== 'admin' ? HOST_ROLES : ROLES);

	function linkFor(i: UserInvite): string {
		// This computer shared with a team: its network address, not 127.0.0.1.
		const base = teamState.address || (SAME_ORIGIN ? location.origin : connectionState.url);
		return `${base}/?invite=${encodeURIComponent(i.code)}`;
	}

	function showInvite(i: UserInvite) {
		invite = {
			name: i.user.name,
			link: linkFor(i),
			app: i.app_invite,
			problem: i.app_invite_problem,
			until: new Date(i.expires_at).toLocaleString()
		};
	}

	async function load() {
		try {
			people = await listUsers();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not load people');
		}
	}

	async function add() {
		busy = true;
		try {
			showInvite(await addUser(name.trim(), email.trim(), role));
			name = email = '';
			role = 'member';
			await load();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message.replace(/^\d+: /, '') : 'Could not add them');
		} finally {
			busy = false;
		}
	}

	async function change(p: UserInfo, update: { role?: string; disabled?: boolean }) {
		try {
			await changeUser(p.id, update);
		} catch (e) {
			toastState.error(e instanceof Error ? e.message.replace(/^\d+: /, '') : 'Could not change that');
		}
		await load();
	}

	async function newInvite(p: UserInfo) {
		try {
			showInvite(await inviteUser(p.id));
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not make an invite');
		}
	}

	async function signOutEverywhere(p: UserInfo) {
		if (!confirm(`Sign ${p.name} out of every browser? They will need a new invite link.`)) return;
		await signOutUser(p.id);
		toastState.info(`${p.name} is signed out everywhere`);
		await load();
	}

	function seen(p: UserInfo): string {
		return p.last_seen_at ? new Date(p.last_seen_at).toLocaleDateString() : 'never';
	}

	onMount(load);
</script>

<section aria-label="People and access">
	<h3 class="text-lg font-semibold text-gray-200 mb-1">People and access</h3>
	<p class="text-xs text-gray-500 mb-3">
		{#if hosting}
			Everyone who signs in to this computer. Members see only their own meetings; reviewers read the team's meetings,
			and yours only when you share them. You are the only admin.
		{:else}
			Everyone who uses this server. Members see only their own meetings, reviewers read all of them, admins also manage
			people and settings.
		{/if}
		Nobody has a password: each invite link signs one browser in, works once and expires after a week. Opening, playing
		and exporting a meeting is noted in its history.
	</p>

	{#if invite}
		<div class="mb-3 rounded border border-blue-800 bg-blue-950/30 p-3 text-sm space-y-2">
			{#if invite.app}
				<p class="text-gray-200">Invite for {invite.name}:</p>
				<div class="space-y-1">
					<p class="text-xs text-gray-400">In a browser</p>
					<code class="block break-all text-xs text-gray-300">{invite.link}</code>
					<button onclick={() => copyText(invite!.link, 'Invite link copied')} class="text-blue-400 hover:text-blue-300">Copy</button>
				</div>
				<div class="space-y-1">
					<p class="text-xs text-gray-400">In the Mnemosyne app</p>
					<code class="block break-all text-xs text-gray-300">{invite.app}</code>
					<button onclick={() => copyText(invite!.app!, 'App invite copied')} class="text-blue-400 hover:text-blue-300">Copy</button>
					<p class="text-xs text-gray-500">
						They paste it in the app: Settings → Devices and people → Join a team. It works from anywhere, and they never join your network.
					</p>
				</div>
				<p class="text-xs text-gray-500">Either one works, once, until {invite.until}; whichever they use first uses it up.</p>
			{:else}
				<p class="text-gray-200">Invite link for {invite.name} (works once, until {invite.until}):</p>
				<code class="block break-all text-xs text-gray-300">{invite.link}</code>
				<button onclick={() => copyText(invite!.link, 'Invite link copied')} class="text-blue-400 hover:text-blue-300">Copy</button>
				{#if invite.problem}<p class="text-xs text-gray-500">No app invite: {invite.problem}</p>{/if}
			{/if}
			<div class="flex gap-3">
				<button onclick={() => (invite = null)} class="text-gray-400 hover:text-gray-200">Done</button>
			</div>
			<p class="text-xs text-gray-500">Send it to them directly (not in a shared channel): whoever opens it first signs in as them.</p>
		</div>
	{/if}

	<table class="w-full text-sm mb-3">
		<thead class="text-left text-xs text-gray-500">
			<tr><th class="py-1 font-normal">Name</th><th class="font-normal">Role</th><th class="font-normal">Last seen</th><th></th></tr>
		</thead>
		<tbody>
			{#each people as p (p.id)}
				<tr class="border-t border-gray-800 {p.disabled ? 'opacity-50' : ''}">
					<td class="py-1.5">
						<span class="text-gray-200">{p.name}</span>
						{#if p.email}<span class="ml-1 text-xs text-gray-500">{p.email}</span>{/if}
						{#if p.id === connectionState.me?.id}<span class="ml-1 text-xs text-gray-500">(you)</span>{/if}
						{#if p.devices.length}<span class="block text-xs text-gray-500">{p.devices.map((d) => d.device).join(', ')}</span>{/if}
					</td>
					<td>
						<select value={p.role} onchange={(e) => change(p, { role: e.currentTarget.value })} aria-label="Role of {p.name}" class="bg-gray-800 border border-gray-700 rounded px-1.5 py-0.5 text-xs text-gray-200">
							{#each rolesFor(p.role) as [value]}<option {value}>{value}</option>{/each}
						</select>
					</td>
					<td class="text-xs text-gray-400">{seen(p)}</td>
					<td class="text-right text-xs space-x-3 whitespace-nowrap">
						<button onclick={() => newInvite(p)} class="text-blue-400 hover:text-blue-300">Invite link</button>
						<button onclick={() => signOutEverywhere(p)} class="text-gray-400 hover:text-gray-200">Sign out everywhere</button>
						<button onclick={() => change(p, { disabled: !p.disabled })} class="text-gray-400 hover:text-gray-200">{p.disabled ? 'Enable' : 'Disable'}</button>
					</td>
				</tr>
			{/each}
		</tbody>
	</table>

	<form
		onsubmit={(e) => {
			e.preventDefault();
			add();
		}}
		class="flex flex-wrap items-end gap-2"
		aria-label="Add a person"
	>
		<label class="text-xs text-gray-400">Name<input bind:value={name} required class="block mt-1 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-100" /></label>
		<label class="text-xs text-gray-400">Email<input bind:value={email} type="email" class="block mt-1 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-100" /></label>
		<label class="text-xs text-gray-400">Role
			<select bind:value={role} class="block mt-1 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-100">
				{#each rolesFor('member') as [value, label]}<option {value}>{label}</option>{/each}
			</select>
		</label>
		<button type="submit" disabled={busy || !name.trim()} class="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200 disabled:opacity-50">Add and invite</button>
	</form>
</section>
