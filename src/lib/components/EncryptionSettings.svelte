<script lang="ts">
	import { disableEncryption, enableEncryption, getEncryption } from '$lib/api/backend.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { EncryptionEnabled, EncryptionStatus } from '$lib/types/index.js';

	let status = $state<EncryptionStatus | null>(null);
	let busy = $state(false);
	let confirming = $state<'enable' | 'disable' | null>(null);
	let result = $state<EncryptionEnabled | null>(null);
	let saved = $state(false);

	$effect(() => {
		getEncryption()
			.then((s) => (status = s))
			.catch(() => {});
	});

	async function enable() {
		busy = true;
		try {
			result = await enableEncryption();
			status = { enabled: true, locked: false };
			confirming = null;
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not turn encryption on');
		} finally {
			busy = false;
		}
	}

	async function disable() {
		busy = true;
		try {
			status = await disableEncryption();
			confirming = null;
			toastState.success('Meetings are no longer encrypted');
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not turn encryption off');
		} finally {
			busy = false;
		}
	}

	async function copyCode() {
		if (!result) return;
		await navigator.clipboard.writeText(result.recovery_code);
		toastState.success('Recovery code copied');
	}
</script>

{#if status}
	<section>
		<h3 class="text-lg font-semibold text-gray-200 mb-1">Encryption</h3>
		<p class="text-xs text-gray-500 mb-3">
			Encrypts the meetings on this computer: transcripts, summaries, notes, search index, voice profiles and every
			recording once it has stopped. The key is kept in your system keyring and unlocks with your login. Not
			encrypted: audio while it is being recorded, notes exported to Obsidian, the settings file and the log.
		</p>
		<div class="flex flex-wrap items-center gap-3">
			<span class="text-sm {status.enabled ? 'text-green-400' : 'text-gray-400'}">
				{status.enabled ? 'On: meetings are encrypted' : 'Off'}
			</span>
			{#if confirming === null}
				<button
					onclick={() => (confirming = status?.enabled ? 'disable' : 'enable')}
					class="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300"
				>{status.enabled ? 'Turn off…' : 'Encrypt meetings…'}</button>
			{/if}
		</div>

		{#if confirming === 'enable'}
			<div class="mt-3 rounded border border-gray-700 bg-gray-900 p-3 space-y-2 text-sm text-gray-300">
				<p>
					All meetings will be encrypted now; this can take a while with many recordings. You will get a recovery code:
					if the keyring ever loses the key (new computer, reset keyring), it is the <strong>only</strong> way back
					to your meetings.
				</p>
				<div class="flex gap-2">
					<button onclick={enable} disabled={busy} class="px-3 py-1.5 text-sm rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50">
						{busy ? 'Encrypting…' : 'Encrypt'}
					</button>
					<button onclick={() => (confirming = null)} disabled={busy} class="px-3 py-1.5 text-sm text-gray-400 hover:text-gray-200">Cancel</button>
				</div>
			</div>
		{:else if confirming === 'disable'}
			<div class="mt-3 rounded border border-gray-700 bg-gray-900 p-3 space-y-2 text-sm text-gray-300">
				<p>Decrypt every meeting and remove the key from the keyring?</p>
				<div class="flex gap-2">
					<button onclick={disable} disabled={busy} class="px-3 py-1.5 text-sm rounded bg-gray-700 hover:bg-gray-600 text-gray-100 disabled:opacity-50">
						{busy ? 'Decrypting…' : 'Decrypt'}
					</button>
					<button onclick={() => (confirming = null)} disabled={busy} class="px-3 py-1.5 text-sm text-gray-400 hover:text-gray-200">Cancel</button>
				</div>
			</div>
		{/if}
	</section>
{/if}

{#if result}
	<div class="fixed inset-0 z-40 flex items-start justify-center bg-black/60 px-4 pt-[12vh]" role="presentation">
		<div class="w-full max-w-lg space-y-3 rounded-lg border border-gray-700 bg-gray-900 p-5" role="dialog" aria-modal="true" aria-label="Recovery code">
			<h3 class="text-lg font-semibold text-gray-100">Save your recovery code</h3>
			<p class="text-sm text-gray-400">
				Meetings are encrypted ({result.files} recording file{result.files === 1 ? '' : 's'}). Keep this code somewhere
				safe, like a password manager. It is shown only now.
			</p>
			{#if result.errors.length}
				<p class="text-sm text-amber-300">{result.errors.length} file(s) could not be encrypted: {result.errors.join('; ')}</p>
			{/if}
			<div class="rounded bg-gray-950 border border-gray-800 p-3 font-mono text-sm text-gray-100 break-all select-all" aria-label="The recovery code">
				{result.recovery_code}
			</div>
			<div class="flex items-center gap-3">
				<button onclick={copyCode} class="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200">Copy</button>
				<label class="flex items-center gap-2 text-sm text-gray-300">
					<input type="checkbox" bind:checked={saved} class="rounded border-gray-600 bg-gray-800" />
					I have saved it
				</label>
				<button
					onclick={() => {
						result = null;
						saved = false;
					}}
					disabled={!saved}
					class="ml-auto px-3 py-1.5 text-sm rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50"
				>Done</button>
			</div>
		</div>
	</div>
{/if}
