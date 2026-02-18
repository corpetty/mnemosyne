<script lang="ts">
	import { exportToObsidian, getVaultConfig, setVaultConfig } from '$lib/api/backend.js';
	import { sessionState } from '$lib/stores/session.svelte.js';

	let vaultPath = $state('');
	let subfolder = $state('meetings/mnemosyne');
	let vaultExists = $state(false);
	let exporting = $state(false);
	let exportResult = $state('');
	let error = $state('');
	let configLoaded = $state(false);

	async function loadConfig() {
		try {
			const config = await getVaultConfig();
			vaultPath = config.vault_path;
			subfolder = config.subfolder;
			vaultExists = config.exists;
			configLoaded = true;
		} catch (e) {
			console.error('Failed to load vault config:', e);
		}
	}

	async function saveConfig() {
		error = '';
		try {
			const config = await setVaultConfig(vaultPath, subfolder);
			vaultExists = config.exists;
			if (!vaultExists) {
				error = 'Vault path does not exist';
			}
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to save config';
		}
	}

	async function pickDirectory() {
		try {
			// Use Tauri dialog if available, fallback to manual input
			const { open } = await import('@tauri-apps/plugin-dialog');
			const selected = await open({ directory: true, title: 'Select Obsidian Vault' });
			if (selected) {
				vaultPath = selected;
				await saveConfig();
			}
		} catch {
			// Not in Tauri environment, user edits manually
			console.log('Directory picker not available (not running in Tauri)');
		}
	}

	async function handleExport() {
		const session = sessionState.activeSession;
		if (!session) return;

		exporting = true;
		error = '';
		exportResult = '';
		try {
			const result = await exportToObsidian(session.id);
			exportResult = result.path;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Export failed';
		} finally {
			exporting = false;
		}
	}

	$effect(() => {
		if (!configLoaded) {
			loadConfig();
		}
	});
</script>

<div class="space-y-3">
	<!-- Vault config -->
	<div class="flex items-center gap-2">
		<input
			type="text"
			bind:value={vaultPath}
			placeholder="/path/to/obsidian/vault"
			class="flex-1 bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-200 placeholder-gray-600"
		/>
		<button
			onclick={pickDirectory}
			class="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 transition-colors"
		>
			Browse
		</button>
		<button
			onclick={saveConfig}
			class="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 transition-colors"
		>
			Save
		</button>
	</div>

	<div class="flex items-center gap-2">
		<label class="text-xs text-gray-500">
			Subfolder:
			<input
				type="text"
				bind:value={subfolder}
				class="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-300 w-48"
			/>
		</label>
		{#if vaultPath}
			<span class="text-xs {vaultExists ? 'text-green-500' : 'text-red-400'}">
				{vaultExists ? 'Vault found' : 'Vault not found'}
			</span>
		{/if}
	</div>

	<!-- Export button -->
	<button
		onclick={handleExport}
		disabled={exporting || !vaultPath || !vaultExists}
		class="px-4 py-1.5 text-sm rounded bg-indigo-600 hover:bg-indigo-700 disabled:bg-gray-700 disabled:text-gray-500 text-white font-medium transition-colors"
	>
		{exporting ? 'Exporting...' : 'Export to Obsidian'}
	</button>

	{#if exportResult}
		<p class="text-green-400 text-sm">Exported to: <code class="text-green-300">{exportResult}</code></p>
	{/if}

	{#if error}
		<p class="text-red-400 text-sm">{error}</p>
	{/if}
</div>
