<script lang="ts">
	import { unlockEncryption } from '$lib/api/backend.js';

	let code = $state('');
	let busy = $state(false);
	let error = $state('');

	async function unlock() {
		busy = true;
		error = '';
		try {
			await unlockEncryption(code);
			window.location.reload(); // start the app normally, now that the meetings open
		} catch (e) {
			error = e instanceof Error ? e.message.replace(/^\d+: /, '') : 'Could not unlock';
			busy = false;
		}
	}
</script>

<div class="flex-1 flex items-center justify-center px-4">
	<form
		onsubmit={(e) => {
			e.preventDefault();
			unlock();
		}}
		class="w-full max-w-md space-y-3 rounded-lg border border-gray-800 bg-gray-900 p-5"
		aria-label="Unlock meetings"
	>
		<h2 class="text-lg font-semibold text-gray-100">Your meetings are locked</h2>
		<p class="text-sm text-gray-400">
			They are encrypted, and the key is not in this computer's keyring (a new computer, a reset keyring, or a
			different user). Enter the recovery code you saved when you turned encryption on.
		</p>
		<input
			bind:value={code}
			placeholder="XXXX-XXXX-…"
			aria-label="Recovery code"
			autocomplete="off"
			spellcheck="false"
			class="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 font-mono text-sm text-gray-100"
		/>
		{#if error}<p class="text-sm text-red-400">{error}</p>{/if}
		<button type="submit" disabled={busy || !code.trim()} class="w-full px-3 py-1.5 text-sm rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50">
			{busy ? 'Unlocking…' : 'Unlock'}
		</button>
	</form>
</div>
