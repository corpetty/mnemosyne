<script lang="ts">
	// Settings → Server mode: this backend's remote access (services/link.py) and pairing a
	// computer with it. The toggle itself is the remote_access setting, saved with the others.
	import { createPairingCode, getRemoteAccess } from '$lib/api/backend.js';
	import type { PairingCode, RemoteAccess } from '$lib/types/index.js';

	/** Saved settings this depends on: remote_access and whether an API token is set. */
	let { enabled = false, tokenSet = false }: { enabled?: boolean; tokenSet?: boolean } = $props();

	let status = $state<RemoteAccess | null>(null);
	let code = $state<PairingCode | null>(null);
	let copied = $state(false);
	let error = $state('');

	// The sidecar takes a few seconds to reach a relay after it is turned on: poll until it runs.
	$effect(() => {
		void [enabled, tokenSet];
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

	async function copy() {
		if (!code?.invite) return;
		await navigator.clipboard.writeText(code.invite);
		copied = true;
	}

	const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
</script>

<div class="mt-4 space-y-2" aria-label="Remote access to this backend">
	<h4 class="text-sm font-medium text-gray-300">Remote access</h4>
	<p class="text-xs text-gray-500">
		{#if !status?.enabled}
			Off. When on, computers you pair reach this backend from anywhere over an end-to-end encrypted connection;
			nothing listens on the internet.
		{:else if status.running}
			On. Paired computers can connect; relays only pass along encrypted traffic.
		{:else if status.error}
			<span class="text-red-400">{status.error}</span>
		{:else}
			Starting…
		{/if}
	</p>
	{#if status?.running}
		{#if !tokenSet}
			<p class="text-xs text-yellow-500">Set an API token above to pair computers.</p>
		{:else}
			<div class="flex items-center gap-3">
				<button
					onclick={pairComputer}
					class="px-3 py-1.5 text-xs rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200"
					>{code ? 'New invite' : 'Pair a computer'}</button
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
					Paste it on the other computer in Settings → Connection → Remote access. Works once, until
					{clock(code.expires_at)}. That computer gets its own key; remove it from the list below at any time.
				</p>
			{/if}
		{/if}
	{/if}
</div>
