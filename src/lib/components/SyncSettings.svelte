<script lang="ts">
	// Settings → Connection: keep one library across this person's computers (backend
	// services/sync_service.py, docs/api-reference.md "Library sync").
	import { createPairingCode, removeSyncPeer, syncNow, syncPair } from '$lib/api/backend.js';
	import { peerState } from '$lib/app/syncText.js';
	import { syncState } from '$lib/stores/sync.svelte.js';
	import type { PairingCode } from '$lib/types/index.js';

	let { enabled = false, tokenSet = false }: { enabled?: boolean; tokenSet?: boolean } = $props();

	let code = $state<PairingCode | null>(null);
	let copied = $state(false);
	let invite = $state('');
	let joining = $state(false);
	let error = $state('');
	let now = $state(Date.now());

	$effect(() => {
		if (enabled && tokenSet) void syncState.load();
	});
	// Relative times move on.
	$effect(() => {
		const t = setInterval(() => (now = Date.now()), 30_000);
		return () => clearInterval(t);
	});

	const clashes = $derived(syncState.peers.reduce((n, p) => n + p.clashes.length, 0));

	async function makeInvite() {
		error = '';
		copied = false;
		try {
			code = await createPairingCode('sync');
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not create an invite';
		}
	}

	async function copy() {
		if (!code?.invite) return;
		await navigator.clipboard.writeText(code.invite);
		copied = true;
	}

	async function join() {
		if (!invite.trim() || joining) return;
		joining = true;
		error = '';
		try {
			await syncPair(invite.trim(), syncState.status?.self.name ?? '');
			invite = '';
			await syncState.load();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not pair';
		} finally {
			joining = false;
		}
	}

	async function pullNow() {
		error = '';
		try {
			await syncNow();
			await syncState.load();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not start syncing';
		}
	}

	async function remove(id: string, name: string) {
		if (!confirm(`Stop syncing with ${name}? Meetings already synced stay on both computers.`)) return;
		error = '';
		try {
			await removeSyncPeer(id);
			await syncState.load();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not remove the computer';
		}
	}

	const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
</script>

<div class="mt-4 space-y-2" aria-label="Sync with your other computers">
	<h4 class="text-sm font-medium text-gray-300">Sync with your other computers</h4>
	<p class="text-xs text-gray-500">
		Meetings recorded on either computer appear on both; summaries and notes merge. Audio stays on the computer that recorded it.
	</p>
	{#if !enabled || !tokenSet}
		<p class="text-xs text-gray-500">Turn on remote access and set an API token above to sync.</p>
	{:else}
		{#if syncState.status}
			<p class="text-xs text-gray-400">This computer: <span class="text-gray-200">{syncState.status.self.name}</span></p>
		{/if}

		<div class="flex items-center gap-3">
			<button
				onclick={makeInvite}
				class="px-3 py-1.5 text-xs rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200"
				>Invite a computer</button
			>
			{#if error}<span class="text-xs text-red-400">{error}</span>{/if}
		</div>
		{#if code?.invite}
			<div class="flex items-center gap-2 max-w-2xl">
				<input
					readonly
					value={code.invite}
					aria-label="Sync invite for the other computer"
					class="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-xs text-gray-300 w-full font-mono"
				/>
				<button onclick={copy} class="text-xs text-gray-400 hover:text-gray-200 whitespace-nowrap">{copied ? 'Copied' : 'Copy'}</button>
			</div>
			<p class="text-xs text-gray-600">
				Paste it under Sync on the other computer. Works once, until {clock(code.expires_at)}.
			</p>
		{/if}

		<div class="flex items-center gap-2 max-w-2xl">
			<input
				type="text"
				bind:value={invite}
				placeholder="Paste an invite"
				aria-label="Paste an invite from the other computer"
				onkeydown={(e) => e.key === 'Enter' && join()}
				class="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-xs text-gray-300 w-full font-mono"
			/>
			<button
				onclick={join}
				disabled={!invite.trim() || joining}
				class="px-3 py-1.5 text-xs rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200 disabled:opacity-50"
				>{joining ? 'Syncing…' : 'Sync'}</button
			>
		</div>

		{#if syncState.peers.length}
			<ul class="max-w-2xl divide-y divide-gray-800 rounded border border-gray-800">
				{#each syncState.peers as p (p.id)}
					<li class="flex items-center gap-3 px-2 py-1.5 text-xs">
						<span class="text-gray-200">{p.name}</span>
						<span class="font-mono text-gray-600" title={p.endpoint}>{p.endpoint.slice(0, 8)}</span>
						<span class="min-w-0 flex-1 truncate {p.state === 'error' || p.state === 'skew' ? 'text-amber-300' : 'text-gray-500'}" title={peerState(p, now)}>{peerState(p, now)}</span>
						<button
							onclick={() => pullNow()}
							disabled={p.state === 'syncing'}
							class="text-gray-400 hover:text-gray-200 disabled:opacity-50 whitespace-nowrap">Sync now</button
						>
						<button onclick={() => remove(p.id, p.name)} class="text-gray-500 hover:text-red-400">Remove</button>
					</li>
				{/each}
			</ul>
		{/if}
		{#if clashes}
			<p class="text-xs text-gray-600">
				{clashes} meeting{clashes === 1 ? '' : 's'} could not be synced (same id on both computers)
			</p>
		{/if}
	{/if}
</div>
