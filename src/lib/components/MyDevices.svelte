<script lang="ts">
	// Settings → Devices and people → My devices (main computer only): everything connected to
	// this computer in one list (GET /api/connections/devices), and Add a device.
	import type { Snippet } from 'svelte';
	import { ago } from '$lib/app/syncText.js';
	import { devicesState } from '$lib/stores/devices.svelte.js';
	import { syncState } from '$lib/stores/sync.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { ConnectedDevice } from '$lib/types/index.js';
	import PhoneLink from './PhoneLink.svelte';
	import RemoteAccessHome from './RemoteAccessHome.svelte';
	import SyncSettings from './SyncSettings.svelte';

	/** The saved remote-access settings and phone address (what the sections below need). */
	let {
		remoteAccess = false,
		relays = '',
		ourServers = true,
		phoneUrl = '',
		phoneAddress
	}: {
		remoteAccess?: boolean;
		relays?: string;
		ourServers?: boolean;
		phoneUrl?: string;
		/** The Phone address field (a setting, saved with the panel's Save). */
		phoneAddress?: Snippet;
	} = $props();

	let adding = $state<'choose' | 'computer' | 'phone' | null>(null);
	let now = $state(Date.now());

	// Contacts are people, listed under People.
	const mine = $derived((devicesState.list ?? []).filter((d) => d.kind !== 'contact'));

	// Loaded on opening, and again when a sync peer comes or goes in "More".
	$effect(() => {
		void syncState.peers.length;
		void devicesState.load();
	});
	$effect(() => {
		const t = setInterval(() => (now = Date.now()), 30_000);
		return () => clearInterval(t);
	});

	async function remove(d: ConnectedDevice) {
		const ok =
			d.kind === 'sync'
				? confirm(`Stop syncing with ${d.name}? Meetings already synced stay on both computers.`)
				: confirm(`Remove ${d.name}? It can no longer connect to this computer.`);
		if (!ok) return;
		try {
			await devicesState.remove(d);
			if (d.kind === 'sync') void syncState.load();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not remove the device');
		}
	}
</script>

<section aria-label="My devices">
	<div class="flex items-center gap-3 mb-1">
		<h3 class="text-lg font-semibold text-gray-200">My devices</h3>
		<button
			onclick={() => (adding = adding ? null : 'choose')}
			class="ml-auto px-3 py-1.5 text-xs rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200"
			>Add a device</button
		>
	</div>
	<RemoteAccessHome enabled={remoteAccess} {relays} {ourServers} part="status" />

	{#if adding}
		<div class="mt-3 rounded border border-gray-800 bg-gray-900 p-3 space-y-3" aria-label="Add a device">
			<div class="flex flex-wrap items-center gap-2">
				<button
					onclick={() => (adding = 'computer')}
					aria-pressed={adding === 'computer'}
					class="px-3 py-1.5 text-sm rounded border {adding === 'computer' ? 'border-blue-600 bg-blue-950/30 text-gray-100' : 'border-gray-700 text-gray-300 hover:text-gray-100'}"
					>A laptop or another computer</button
				>
				<button
					onclick={() => (adding = 'phone')}
					aria-pressed={adding === 'phone'}
					class="px-3 py-1.5 text-sm rounded border {adding === 'phone' ? 'border-blue-600 bg-blue-950/30 text-gray-100' : 'border-gray-700 text-gray-300 hover:text-gray-100'}"
					>A phone</button
				>
				<button onclick={() => (adding = null)} class="ml-auto text-xs text-gray-500 hover:text-gray-300">Close</button>
			</div>
			{#if adding === 'computer'}
				<RemoteAccessHome enabled={remoteAccess} {relays} {ourServers} part="invite" />
			{:else if adding === 'phone'}
				{#if phoneAddress}{@render phoneAddress()}{/if}
				<PhoneLink {phoneUrl} />
			{/if}
		</div>
	{/if}

	{#if devicesState.list === null}
		<p class="mt-3 text-xs text-gray-500">Loading…</p>
	{:else if mine.length === 0}
		<p class="mt-3 text-xs text-gray-500">Nothing is connected yet. Add a laptop, another computer or a phone.</p>
	{:else}
		<ul class="mt-3 max-w-2xl divide-y divide-gray-800 rounded border border-gray-800" aria-label="My devices">
			{#each mine as d (d.kind + d.id)}
				<li class="flex items-center gap-3 px-3 py-2 text-xs">
					<span class="text-gray-500" title={d.kind === 'phone' ? 'Phone' : 'Computer'}>
						{#if d.kind === 'phone'}
							<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
								<rect x="7" y="2.5" width="10" height="19" rx="2" />
								<path d="M11 18.5h2" />
							</svg>
						{:else}
							<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
								<rect x="4.5" y="5" width="15" height="10.5" rx="1.5" />
								<path d="M2.5 18.5h19" />
							</svg>
						{/if}
					</span>
					<span class="text-sm text-gray-200">{d.name}</span>
					<span class="min-w-0 flex-1 truncate text-gray-500">{d.can}</span>
					<span class="text-gray-600 whitespace-nowrap">{d.last_seen_at ? `seen ${ago(d.last_seen_at, now)}` : 'not seen yet'}</span>
					<button onclick={() => remove(d)} class="text-gray-500 hover:text-red-400">Remove</button>
				</li>
			{/each}
		</ul>
	{/if}

	<details class="mt-3">
		<summary class="cursor-pointer text-xs text-gray-400 hover:text-gray-200">
			More: another main computer that keeps its own library in sync
		</summary>
		<SyncSettings />
	</details>
</section>
