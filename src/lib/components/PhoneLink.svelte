<script lang="ts">
	import { codeLength, until } from '$lib/app/codeLength.svelte.js';
	import CodeLength from './CodeLength.svelte';
	import { createPairingCode, getPhoneLink, listPairedDevices } from '$lib/api/backend.js';
	import { devicesState } from '$lib/stores/devices.svelte.js';
	import type { PairedDevice, PairingCode, PhoneLink } from '$lib/types/index.js';

	/** The saved phone address. Pairing a phone makes the API token it needs. */
	let { phoneUrl = '' }: { phoneUrl?: string } = $props();

	let link = $state<PhoneLink | null>(null);
	let devices = $state<PairedDevice[]>([]);
	let code = $state<PairingCode | null>(null);
	let justPaired = $state('');
	let error = $state('');
	let qr = $state('');
	let chosen = $state(0);

	async function loadDevices() {
		try {
			devices = await listPairedDevices();
		} catch {
			devices = [];
		}
	}

	$effect(() => {
		void phoneUrl;
		getPhoneLink()
			.then((l) => {
				link = l;
				if (l.pairing) loadDevices();
			})
			.catch(() => (link = null));
	});

	// Addresses on offer: with pairing, the ones carrying the one-time code.
	let urls = $derived(link?.pairing ? (code?.urls ?? []) : (link?.urls ?? []));

	$effect(() => {
		const url = urls[chosen] ?? urls[0];
		if (!url || !link?.reachable) {
			qr = '';
			return;
		}
		import('qrcode').then((QR) =>
			QR.toDataURL(url, { margin: 1, width: 200, color: { dark: '#030712', light: '#f3f4f6' } }).then((d) => (qr = d))
		);
	});

	// While a code is showing, watch for the phone that redeems it; stop when it expires.
	$effect(() => {
		if (!code) return;
		const expires = Date.parse(code.expires_at);
		const known = new Set(devices.map((d) => d.id));
		const timer = setInterval(async () => {
			if (Date.now() > expires) {
				code = null;
				return;
			}
			const now = await listPairedDevices().catch(() => null);
			const added = now?.find((d) => !known.has(d.id));
			if (now && added) {
				devices = now;
				justPaired = added.name;
				code = null;
				void devicesState.load();
			}
		}, 2000);
		return () => clearInterval(timer);
	});

	async function showCode() {
		error = '';
		justPaired = '';
		chosen = 0;
		try {
			code = await createPairingCode('phone', codeLength.minutes);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not create a pairing code';
		}
	}

</script>

{#if link}
	<div class="mt-4 space-y-2" aria-label="Record from a phone">
		<h4 class="text-sm font-medium text-gray-300">Record from a phone</h4>
		<p class="text-xs text-gray-500">{link.note}</p>
		{#if link.reachable && link.pairing}
			<div class="flex items-center gap-3 text-sm">
				<button
					onclick={showCode}
					class="px-3 py-1.5 text-xs rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200"
				>{code ? 'New pairing code' : 'Pair a phone'}</button>
				<CodeLength />
				{#if justPaired}<span class="text-xs text-green-400">Paired: {justPaired}</span>{/if}
				{#if error}<span class="text-xs text-red-400">{error}</span>{/if}
			</div>
		{/if}
		{#if link.reachable && urls.length}
			<div class="flex flex-wrap items-start gap-4">
				{#if qr}<img src={qr} alt="QR code for the phone recording page" class="w-40 h-40 rounded" />{/if}
				<div class="space-y-1 text-xs">
					<p class="text-gray-400">Scan with the phone, or open one of these on it:</p>
					{#each urls as u, i}
						<button
							onclick={() => (chosen = i)}
							class="block font-mono break-all text-left {chosen === i ? 'text-blue-300' : 'text-gray-500 hover:text-gray-300'}"
						>{u}</button>
					{/each}
					{#if code}
						<p class="text-gray-600">Works once, until {until(code.expires_at)}. The phone gets its own key; the API token stays here.</p>
					{/if}
				</div>
			</div>
		{/if}
	</div>
{/if}
