<script lang="ts">
	import { onMount } from 'svelte';
	import { getTeam, setTeam } from '$lib/api/backend.js';
	import { refreshMe } from '$lib/app/controller.svelte.js';
	import { copyText } from '$lib/app/clipboard.js';
	import { teamState } from '$lib/stores/team.svelte.js';
	import type { TeamStatus } from '$lib/types/index.js';

	// Hosting a team (backend services/team_host.py): this computer, a main computer, also serves
	// the team. Only the desktop app on this computer may turn it on or off. Settings → Devices and
	// people shows it in two places that share one status (`teamState.host`): `role` in "This
	// computer" under Main computer (start, addresses, stop) and `options` in "Your team" (Tailscale
	// name, keep sharing when I quit, keep this computer awake).
	let { part = 'role' }: { part?: 'role' | 'options' } = $props();
	const status = $derived(teamState.host);
	let name = $state('');
	let busy = $state(false);
	let error = $state('');
	let chosen = $state(0);
	let qr = $state('');

	function show(s: TeamStatus) {
		teamState.host = s;
		teamState.address = s.running ? (s.addresses[0] ?? '') : '';
		if (!name) name = s.owner;
	}

	onMount(() => {
		getTeam()
			.then(show)
			.catch(() => (teamState.host = null)); // an older backend
	});

	let url = $derived(status?.addresses[chosen] ?? status?.addresses[0] ?? '');

	$effect(() => {
		if (!url || !status?.running) {
			qr = '';
			return;
		}
		import('qrcode').then((QR) =>
			QR.toDataURL(url, { margin: 1, width: 160, color: { dark: '#030712', light: '#f3f4f6' } }).then((d) => (qr = d))
		);
	});

	async function setOption(
		option: 'keep_sharing_after_quit' | 'keep_awake_while_sharing' | 'tailscale_cert',
		value: boolean
	) {
		error = '';
		busy = true;
		try {
			show(await setTeam({ [option]: value }));
		} catch (e) {
			error = e instanceof Error ? e.message.replace(/^\d+: /, '') : 'Could not change that';
		} finally {
			busy = false;
		}
	}

	async function toggle(enabled: boolean) {
		if (!enabled && !confirm('Stop hosting? People on your team can no longer reach this computer. Their accounts and meetings stay.')) return;
		busy = true;
		error = '';
		try {
			show(await setTeam({ enabled, name: name.trim() }));
			await refreshMe();
			if (enabled) await teamState.load();
		} catch (e) {
			error = e instanceof Error ? e.message.replace(/^\d+: /, '') : 'Could not change team hosting';
		} finally {
			busy = false;
		}
	}
</script>

{#snippet tailscale(status: TeamStatus)}
	{#if status.tailscale_name}
		<div class="text-xs text-gray-400 space-y-1">
			{#if status.tailscale_cert}
				<p>
					Tailscale: <span class="font-mono">{status.tailscale_name}</span> uses its own certificate, renewed by itself.
					<button onclick={() => setOption('tailscale_cert', false)} disabled={busy} class="text-gray-500 hover:text-gray-300 underline">Stop using it</button>
				</p>
			{:else}
				<button
					onclick={() => setOption('tailscale_cert', true)}
					disabled={busy}
					class="px-2 py-1 rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200 disabled:opacity-50"
				>Use my Tailscale name (no browser warning)</button>
				<p class="text-gray-500">
					Teammates on your tailnet then open <span class="font-mono">{status.tailscale_name}</span>, from the office or
					from home. Getting the certificate publishes that name in public certificate logs.
				</p>
			{/if}
			{#if status.tailscale_error}<p class="text-amber-400">{status.tailscale_error}</p>{/if}
		</div>
	{/if}
{/snippet}

{#if status?.can_change && part === 'role'}
	<div aria-label="Team hosting" class="mt-3">
		<p class="text-xs text-gray-500 mb-3">
			People on your network open this computer's address in a browser, sign in with an invite link, and record, read
			and share meetings there. You become the first admin and the meetings you have are yours. Nothing else to
			install; this computer must be on while they use it.
		</p>
		{#if !status.enabled}
			<div class="flex flex-wrap items-end gap-3">
				<label class="block max-w-xs">
					<span class="block text-xs text-gray-500 mb-1">Your name, as your team sees it</span>
					<input
						type="text"
						bind:value={name}
						class="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 w-full"
					/>
				</label>
				<button
					onclick={() => toggle(true)}
					disabled={busy || !name.trim()}
					class="px-3 py-1.5 text-sm rounded bg-blue-700 hover:bg-blue-600 text-white disabled:opacity-50"
				>{busy ? 'Starting…' : 'Host a team'}</button>
			</div>
		{:else}
			<div class="rounded border border-gray-800 bg-gray-900 p-3 text-sm space-y-2">
				{#if status.running}
					<p class="text-gray-300">
						<span class="text-green-500">On</span>, with {status.owner} as admin. Others open:
					</p>
					<div class="flex flex-wrap gap-4">
						<ul class="space-y-1">
							{#each status.addresses as a, i (a)}
								<li class="flex items-center gap-2">
									<button
										onclick={() => (chosen = i)}
										class="font-mono text-xs {i === chosen ? 'text-gray-100' : 'text-gray-500 hover:text-gray-300'}"
									>{a}</button>
									<button onclick={() => copyText(a, 'Address copied')} class="text-xs text-blue-400 hover:text-blue-300">Copy</button>
								</li>
							{/each}
						</ul>
						{#if qr}<img src={qr} alt="QR code for {url}" width="160" height="160" class="rounded" />{/if}
					</div>
					<p class="text-xs text-gray-500">
						{#if status.tailscale_cert && status.addresses[0]?.includes(status.tailscale_name)}
							On your tailnet, the first address has a real certificate: no browser warning. The others have a certificate
							this computer made, so browsers warn the first time: choose to continue (Advanced → Accept).
						{:else}
							The certificate is made by this computer, so browsers warn the first time: choose to continue (Advanced →
							Accept).
						{/if}
						Recording in a browser needs an HTTPS address. Add people under Your team below and send each their invite link.
					</p>
					{#if status.firewall_hint}
						<p class="text-xs text-gray-500">
							If others cannot connect, open the port in the firewall:
							<code class="text-gray-400 select-all">{status.firewall_hint}</code>
						</p>
					{/if}
				{:else}
					<p class="text-amber-400">The team server is on but not listening{status.error ? `: ${status.error}` : ''}.</p>
					<button onclick={() => toggle(true)} disabled={busy} class="text-sm text-blue-400 hover:text-blue-300">Try again</button>
				{/if}
				<button onclick={() => toggle(false)} disabled={busy} class="text-sm text-gray-400 hover:text-red-400">Stop hosting</button>
			</div>
		{/if}
		{#if error}<p class="text-sm text-red-400 mt-2">{error}</p>{/if}
	</div>
{:else if status?.can_change && part === 'options' && status.enabled}
	<div aria-label="Team hosting options" class="space-y-2">
		{#if status.running}{@render tailscale(status)}{/if}
		<label class="flex items-start gap-2 text-sm text-gray-300 pt-1">
			<input
				type="checkbox"
				class="mt-1"
				checked={status.keep_sharing_after_quit}
				onchange={(e) => setOption('keep_sharing_after_quit', e.currentTarget.checked)}
			/>
			<span>
				Keep sharing when I quit Mnemosyne
				<span class="block text-xs text-gray-500">Your team can go on using this computer until you log out or open Mnemosyne and stop hosting.</span>
			</span>
		</label>
		<label class="flex items-start gap-2 text-sm text-gray-300">
			<input
				type="checkbox"
				class="mt-1"
				checked={status.keep_awake_while_sharing}
				disabled={!status.keep_awake_available}
				onchange={(e) => setOption('keep_awake_while_sharing', e.currentTarget.checked)}
			/>
			<span>
				Keep this computer awake while it is shared
				<span class="block text-xs text-gray-500">
					{#if status.keep_awake_available}
						It never sleeps while someone records here, either way; this keeps it awake all the time it is shared.
					{:else}
						Not available here (no systemd-inhibit, as in the Flatpak).
					{/if}
				</span>
			</span>
		</label>
		{#if error}<p class="text-sm text-red-400">{error}</p>{/if}
	</div>
{/if}
