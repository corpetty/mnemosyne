<script lang="ts">
	import { getPhoneLink } from '$lib/api/backend.js';
	import type { PhoneLink } from '$lib/types/index.js';

	let link = $state<PhoneLink | null>(null);
	let qr = $state('');
	let chosen = $state(0);

	$effect(() => {
		getPhoneLink()
			.then((l) => (link = l))
			.catch(() => (link = null));
	});

	$effect(() => {
		const url = link?.urls[chosen];
		if (!url || !link?.reachable) {
			qr = '';
			return;
		}
		import('qrcode').then((QR) =>
			QR.toDataURL(url, { margin: 1, width: 200, color: { dark: '#030712', light: '#f3f4f6' } }).then((d) => (qr = d))
		);
	});
</script>

{#if link}
	<div class="mt-4 space-y-2" aria-label="Record from a phone">
		<h4 class="text-sm font-medium text-gray-300">Record from a phone</h4>
		<p class="text-xs text-gray-500">{link.note}</p>
		{#if link.reachable && link.urls.length}
			<div class="flex flex-wrap items-start gap-4">
				{#if qr}<img src={qr} alt="QR code for the phone recording page" class="w-40 h-40 rounded" />{/if}
				<div class="space-y-1 text-xs">
					<p class="text-gray-400">Open on your phone (same network):</p>
					{#each link.urls as u, i}
						<button
							onclick={() => (chosen = i)}
							class="block font-mono break-all text-left {chosen === i ? 'text-blue-300' : 'text-gray-500 hover:text-gray-300'}"
						>{u}</button>
					{/each}
					<p class="text-gray-600">The address includes your API token: only share it with your own devices.</p>
				</div>
			</div>
		{/if}
	</div>
{/if}
