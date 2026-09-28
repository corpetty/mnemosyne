<script lang="ts">
	// Settings → Connection, desktop app only: reach a backend on another machine through remote
	// access (src-tauri/src/remote.rs). Pairing turns an invite into this computer's own token and
	// a local tunnel; the connection then points at that tunnel.
	import { connectionState, LOCAL_BACKEND } from '$lib/stores/connection.svelte.js';

	interface Status {
		configured: boolean;
		running: boolean;
		url: string | null;
		home: string | null;
	}

	let desktop = $state(false);
	let status = $state<Status | null>(null);
	let invite = $state('');
	let name = $state('');
	let busy = $state(false);
	let error = $state('');

	async function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
		const core = await import('@tauri-apps/api/core');
		return core.invoke<T>(cmd, args);
	}

	$effect(() => {
		import('@tauri-apps/api/core')
			.then(async ({ isTauri }) => {
				desktop = isTauri();
				if (desktop) status = await invoke<Status>('remote_status');
			})
			.catch(() => (desktop = false));
	});

	async function pair() {
		busy = true;
		error = '';
		try {
			const paired = await invoke<{ url: string; token: string }>('remote_pair', {
				invite: invite.trim(),
				name: name.trim()
			});
			connectionState.save(paired.url, paired.token, true);
			location.reload();
		} catch (e) {
			error = String(e);
		} finally {
			busy = false;
		}
	}

	async function disconnect() {
		busy = true;
		try {
			await invoke('remote_forget');
		} finally {
			connectionState.save(LOCAL_BACKEND, '');
			location.reload();
		}
	}
</script>

{#if desktop}
	<div class="mt-4 space-y-2" aria-label="Remote access">
		<h4 class="text-sm font-medium text-gray-300">Remote access</h4>
		{#if connectionState.remote && status?.configured}
			<p class="text-xs text-gray-500">
				Connected through remote access to <span class="text-gray-300 font-mono">{status.home}</span>: end-to-end
				encrypted, from anywhere. The other machine can remove this computer at any time.
			</p>
			<button onclick={disconnect} disabled={busy} class="text-sm text-gray-400 hover:text-red-400 disabled:opacity-50"
				>Disconnect</button
			>
		{:else}
			<p class="text-xs text-gray-500">
				Work with the meetings on another machine from anywhere. On that machine, turn on remote access in Settings →
				Server mode, choose <span class="text-gray-400">Pair a computer</span>, and paste the invite here.
			</p>
			<div class="grid grid-cols-[1fr_12rem] gap-3">
				<label>
					<span class="block text-xs text-gray-500 mb-1">Invite</span>
					<input
						type="text"
						bind:value={invite}
						placeholder="endpoint…#…"
						class="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 w-full font-mono"
					/>
				</label>
				<label>
					<span class="block text-xs text-gray-500 mb-1">This computer's name</span>
					<input
						type="text"
						bind:value={name}
						placeholder="Laptop"
						class="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 w-full"
					/>
				</label>
			</div>
			<div class="flex items-center gap-3">
				<button
					onclick={pair}
					disabled={busy || !invite.includes('#')}
					class="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 disabled:opacity-50"
					>{busy ? 'Connecting…' : 'Connect through remote access'}</button
				>
				{#if error}<span class="text-xs text-red-400">{error}</span>{/if}
			</div>
		{/if}
	</div>
{/if}
