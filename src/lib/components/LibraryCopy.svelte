<script lang="ts">
	// Settings → This computer → Uses my main computer: keep a copy of the main computer's library on
	// this laptop. It is library sync (backend services/sync_service.py) with the main computer
	// through the tunnel this window already uses: this computer's own backend redeems an invite
	// that the main computer made for it, so there is nothing to paste.
	import { createPairingCode } from '$lib/api/backend.js';
	import { getLocalSyncStatus, localRemoveSyncPeer, localSyncNow, localSyncPair } from '$lib/api/local.js';
	import { peerState } from '$lib/app/syncText.js';
	import { satelliteState } from '$lib/stores/satellite.svelte.js';
	import type { SyncPeerStatus } from '$lib/types/index.js';

	const KEY = 'mnemosyne.libraryCopy';
	let peers = $state<SyncPeerStatus[]>([]);
	let loaded = $state(false);
	let busy = $state(false);
	let error = $state('');
	let now = $state(Date.now());

	function remembered(): string {
		try {
			return localStorage.getItem(KEY) ?? '';
		} catch {
			return '';
		}
	}
	function remember(id: string) {
		try {
			if (id) localStorage.setItem(KEY, id);
			else localStorage.removeItem(KEY);
		} catch {
			/* no storage */
		}
	}

	// The sync peer that is the main computer: the one made here, else the one with its name.
	const peer = $derived(
		peers.find((p) => p.id === remembered()) ?? peers.find((p) => p.name === satelliteState.mainName) ?? null
	);

	async function load() {
		try {
			peers = (await getLocalSyncStatus()).peers;
			loaded = true;
			if (!error.startsWith('This computer')) return;
			error = '';
		} catch {
			loaded = true;
			error = "This computer's own backend did not answer.";
		}
	}

	$effect(() => {
		void load();
		const t = setInterval(() => {
			now = Date.now();
			void load();
		}, 5000);
		return () => clearInterval(t);
	});

	async function turnOn() {
		busy = true;
		error = '';
		try {
			const code = await createPairingCode('sync'); // asked of the main computer, through the tunnel
			if (!code.invite) throw new Error('The main computer made no invite');
			const { peer: made } = await localSyncPair(code.invite);
			remember(made.id);
			await load();
			void localSyncNow().catch(() => {});
		} catch (e) {
			error = e instanceof Error ? e.message.replace(/^\d+: /, '') : 'Could not start keeping a copy';
		} finally {
			busy = false;
		}
	}

	async function turnOff() {
		if (!peer) return;
		if (!confirm('Stop keeping a copy of the library on this laptop? Meetings already copied stay here.')) return;
		busy = true;
		error = '';
		try {
			await localRemoveSyncPeer(peer.id);
			remember('');
			await load();
		} catch (e) {
			error = e instanceof Error ? e.message.replace(/^\d+: /, '') : 'Could not stop keeping a copy';
		} finally {
			busy = false;
		}
	}
</script>

<div class="mt-4 space-y-1" aria-label="Library copy">
	<label class="flex items-center gap-2 text-sm text-gray-200 {busy ? 'opacity-60' : 'cursor-pointer'}">
		<input
			type="checkbox"
			role="switch"
			checked={!!peer}
			disabled={busy || !loaded}
			onchange={(e) => ((e.currentTarget as HTMLInputElement).checked ? turnOn() : turnOff())}
			class="rounded border-gray-600 bg-gray-800 text-blue-500"
		/>
		Keep a copy of the library on this laptop
	</label>
	<p class="text-xs text-gray-500">
		{satelliteState.mainName} stays the main computer. The copy lets you read your meetings and notes when it cannot be
		reached. Audio stays on the computer that recorded it.
	</p>
	{#if peer}
		<p class="text-xs text-gray-400">{peerState(peer, now)}</p>
	{/if}
	{#if busy}<p class="text-xs text-gray-500">Working…</p>{/if}
	{#if error}<p class="text-xs text-red-400">{error}</p>{/if}
</div>
