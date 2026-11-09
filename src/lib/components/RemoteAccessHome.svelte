<script lang="ts">
	// Settings → Devices and people → My devices: this backend's remote access (services/link.py)
	// and the invite for another computer. `status` is the small line under the heading while
	// remote access is on; `invite` is the "A laptop or another computer" choice of Add a device.
	// The switch itself is the remote_access setting (Advanced), saved with the others.
	import { onMount } from 'svelte';
	import { createPairingCode, getRemoteAccess } from '$lib/api/backend.js';
	import { devicesState } from '$lib/stores/devices.svelte.js';
	import type { PairingCode, RemoteAccess } from '$lib/types/index.js';

	/** Saved settings this depends on: remote_access and remote_relays. Pairing a computer turns
	 * remote access on itself, with the API token it needs. */
	let {
		enabled = false,
		relays = '',
		ourServers = true,
		part = 'status'
	}: { enabled?: boolean; relays?: string; ourServers?: boolean; part?: 'status' | 'invite' } = $props();

	let status = $state<RemoteAccess | null>(null);
	let code = $state<PairingCode | null>(null);
	let copied = $state(false);
	let error = $state('');

	// The sidecar takes a few seconds to reach a relay after it is turned on: poll until it runs.
	$effect(() => {
		void [enabled, relays];
		if (part !== 'status') return;
		let stop = false;
		const poll = async () => {
			for (let i = 0; i < 30 && !stop; i++) {
				status = await getRemoteAccess().catch(() => null);
				if (!status?.enabled || status.running || status.error) return;
				await new Promise((r) => setTimeout(r, 1000));
			}
		};
		poll();
		return () => (stop = true);
	});

	async function pairComputer() {
		error = '';
		copied = false;
		try {
			code = await createPairingCode('desktop');
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not create an invite';
		}
	}

	onMount(() => {
		if (part === 'invite') void pairComputer();
	});

	// While an invite is showing, watch for the computer that redeems it: it joins My devices.
	$effect(() => {
		if (!code) return;
		const expires = Date.parse(code.expires_at);
		const timer = setInterval(() => {
			if (Date.now() > expires) clearInterval(timer);
			else void devicesState.load();
		}, 3000);
		return () => clearInterval(timer);
	});

	async function copy() {
		if (!code?.invite) return;
		await navigator.clipboard.writeText(code.invite);
		copied = true;
	}

	const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
</script>

{#if part === 'status'}
	{#if status?.enabled}
		<p class="text-xs text-gray-500" aria-label="Remote access status">
			{#if status.running}
				On{relays.trim()
					? ', through your relays'
					: ourServers
						? ", through Mnemosyne's relay"
						: ', with no relay (same network only)'}. Your devices can connect{relays.trim() || ourServers
					? '; relays only pass along encrypted traffic.'
					: ' only on the same network or by a direct connection.'}
			{:else if status.error}
				<span class="text-red-400">{status.error}</span>
			{:else}
				Starting…
			{/if}
		</p>
	{/if}
{:else}
	<div class="space-y-2" aria-label="Add a laptop or another computer">
		<div class="flex items-center gap-3">
			<button
				onclick={pairComputer}
				class="px-3 py-1.5 text-xs rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200"
				>New invite</button
			>
			{#if error}<span class="text-xs text-red-400">{error}</span>{/if}
		</div>
		{#if code?.invite}
			<div class="flex items-center gap-2 max-w-2xl">
				<input
					readonly
					value={code.invite}
					aria-label="Invite for the other computer"
					class="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-xs text-gray-300 w-full font-mono"
				/>
				<button onclick={copy} class="text-xs text-gray-400 hover:text-gray-200 whitespace-nowrap"
					>{copied ? 'Copied' : 'Copy'}</button
				>
			</div>
			<p class="text-xs text-gray-600">
				On the other computer, open Settings → Devices and people → This computer, choose
				<span class="text-gray-400">Uses my main computer</span> and paste it. Works once, until
				{clock(code.expires_at)}. That computer gets its own key; remove it from the list above at any time.
			</p>
		{/if}
	</div>
{/if}
