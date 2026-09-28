<script lang="ts">
	import { createPairingCode, getPhoneLink, listPairedDevices, removePairedDevice } from '$lib/api/backend.js';
	import type { PairedDevice, PairingCode, PhoneLink } from '$lib/types/index.js';

	/** Saved settings this depends on: the phone address and whether an API token is set. */
	let { phoneUrl = '', tokenSet = false }: { phoneUrl?: string; tokenSet?: boolean } = $props();

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
		void [phoneUrl, tokenSet];
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
			}
		}, 2000);
		return () => clearInterval(timer);
	});

	async function showCode() {
		error = '';
		justPaired = '';
		chosen = 0;
		try {
			code = await createPairingCode();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not create a pairing code';
		}
	}

	async function remove(d: PairedDevice) {
		try {
			await removePairedDevice(d.id);
			devices = devices.filter((x) => x.id !== d.id);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not remove the device';
		}
	}

	const when = (iso: string) => new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
	const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
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
						<p class="text-gray-600">Works once, until {clock(code.expires_at)}. The phone gets its own key; the API token stays here.</p>
					{/if}
				</div>
			</div>
		{/if}
		{#if link.pairing && devices.length}
			<ul class="space-y-1 text-xs max-w-md" aria-label="Paired phones">
				{#each devices as d (d.id)}
					<li class="flex items-center gap-3">
						<span class="text-gray-300">{d.name}</span>
						<span class="text-gray-600">paired {when(d.created_at)}{d.last_seen_at ? `, last used ${when(d.last_seen_at)}` : ''}</span>
						<button onclick={() => remove(d)} class="ml-auto text-gray-500 hover:text-red-400">Remove</button>
					</li>
				{/each}
			</ul>
		{/if}
	</div>
{/if}
