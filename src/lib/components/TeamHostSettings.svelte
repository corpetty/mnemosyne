<script lang="ts">
	import { onMount } from 'svelte';
	import { getTeam, setTeam } from '$lib/api/backend.js';
	import { refreshMe } from '$lib/app/controller.svelte.js';
	import { copyText } from '$lib/app/clipboard.js';
	import { teamState } from '$lib/stores/team.svelte.js';
	import type { TeamStatus } from '$lib/types/index.js';

	// "Share this computer with my team" (backend services/team_host.py): this computer becomes the
	// team's server. Only the desktop app on this computer may turn it on or off.
	let status = $state<TeamStatus | null>(null);
	let name = $state('');
	let busy = $state(false);
	let error = $state('');
	let chosen = $state(0);
	let qr = $state('');

	function show(s: TeamStatus) {
		status = s;
		teamState.address = s.running ? (s.addresses[0] ?? '') : '';
		if (!name) name = s.owner;
	}

	onMount(() => {
		getTeam()
			.then(show)
			.catch(() => (status = null)); // an older backend
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

	async function setOption(option: 'keep_sharing_after_quit' | 'keep_awake_while_sharing', value: boolean) {
		error = '';
		try {
			show(await setTeam({ [option]: value }));
		} catch (e) {
			error = e instanceof Error ? e.message.replace(/^\d+: /, '') : 'Could not change that';
		}
	}

	async function toggle(enabled: boolean) {
		if (!enabled && !confirm('Stop sharing? People on your team can no longer reach this computer. Their accounts and meetings stay.')) return;
		busy = true;
		error = '';
		try {
			show(await setTeam({ enabled, name: name.trim() }));
			await refreshMe();
			if (enabled) await teamState.load();
		} catch (e) {
			error = e instanceof Error ? e.message.replace(/^\d+: /, '') : 'Could not change sharing';
		} finally {
			busy = false;
		}
	}
</script>

{#if status?.can_change}
	<section aria-label="Share this computer">
		<h3 class="text-lg font-semibold text-gray-200 mb-1">Share this computer with my team</h3>
		<p class="text-xs text-gray-500 mb-3">
			Makes this computer your team's server: people on your network open its address in a browser, sign in with an
			invite link, and record, read and share meetings there. You become the first admin and the meetings you have are
			yours. Nothing else to install; this computer must be on while they use it.
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
				>{busy ? 'Starting…' : 'Share this computer'}</button>
			</div>
		{:else}
			<div class="rounded border border-gray-800 bg-gray-900 p-3 text-sm space-y-2">
				{#if status.running}
					<p class="text-gray-300">
						<span class="text-green-500">Shared</span> as {status.owner} (admin). Others open:
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
						The certificate is made by this computer, so browsers warn the first time: choose to continue (Advanced →
						Accept). Recording in a browser needs that HTTPS address. Add people below and send each their invite link.
					</p>
					{#if status.firewall_hint}
						<p class="text-xs text-gray-500">
							If others cannot connect, open the port in the firewall:
							<code class="text-gray-400 select-all">{status.firewall_hint}</code>
						</p>
					{/if}
				{:else}
					<p class="text-amber-400">Sharing is on but not listening{status.error ? `: ${status.error}` : ''}.</p>
					<button onclick={() => toggle(true)} disabled={busy} class="text-sm text-blue-400 hover:text-blue-300">Try again</button>
				{/if}
				<label class="flex items-start gap-2 text-sm text-gray-300 pt-1">
					<input
						type="checkbox"
						class="mt-1"
						checked={status.keep_sharing_after_quit}
						onchange={(e) => setOption('keep_sharing_after_quit', e.currentTarget.checked)}
					/>
					<span>
						Keep sharing when I quit Mnemosyne
						<span class="block text-xs text-gray-500">Your team can go on using this computer until you log out or open Mnemosyne and stop sharing.</span>
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
				<button onclick={() => toggle(false)} disabled={busy} class="text-sm text-gray-400 hover:text-red-400">Stop sharing</button>
			</div>
		{/if}
		{#if error}<p class="text-sm text-red-400 mt-2">{error}</p>{/if}
	</section>
{/if}
