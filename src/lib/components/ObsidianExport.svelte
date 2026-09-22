<script lang="ts">
	import { exportToObsidian, getExportMarkdown, getSettings, updateSettings } from '$lib/api/backend.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';

	let vaultPath = $state('');
	let subfolder = $state('meetings/mnemosyne');
	let vaultExists = $state(false);
	let lockedByEnv = $state(false);
	let exporting = $state(false);
	let exportResult = $state('');
	let error = $state('');
	let configLoaded = $state(false);
	let preview = $state('');
	let showPreview = $state(false);

	async function loadPreview() {
		const session = sessionState.activeSession;
		if (!session) return;
		try {
			preview = (await getExportMarkdown(session.id)).markdown;
			showPreview = !showPreview || preview !== '';
		} catch (e) {
			error = e instanceof Error ? e.message : 'Preview failed';
		}
	}

	async function copyMarkdown() {
		const session = sessionState.activeSession;
		if (!session) return;
		const md = (await getExportMarkdown(session.id)).markdown;
		await navigator.clipboard.writeText(md);
		toastState.info('Note copied as markdown');
	}

	async function loadConfig() {
		try {
			const s = await getSettings();
			vaultPath = s.values.obsidian_vault_path;
			subfolder = s.values.obsidian_subfolder;
			vaultExists = s.obsidian_vault_exists;
			lockedByEnv = s.env_overrides.includes('obsidian_vault_path');
			configLoaded = true;
		} catch (e) {
			console.error('Failed to load settings:', e);
		}
	}

	async function saveConfig() {
		error = '';
		try {
			const s = await updateSettings({
				obsidian_vault_path: vaultPath,
				obsidian_subfolder: subfolder
			});
			vaultExists = s.obsidian_vault_exists;
			if (vaultPath && !vaultExists) error = 'Vault path does not exist';
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to save settings';
		}
	}

	async function pickDirectory() {
		try {
			const { open } = await import('@tauri-apps/plugin-dialog');
			const selected = await open({ directory: true, title: 'Select Obsidian Vault' });
			if (selected) {
				vaultPath = selected;
				await saveConfig();
			}
		} catch {
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
		if (!configLoaded) loadConfig();
	});
</script>

<div class="space-y-3">
	<div class="flex items-center gap-2">
		<input
			type="text"
			bind:value={vaultPath}
			disabled={lockedByEnv}
			placeholder="/path/to/obsidian/vault"
			class="flex-1 bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-200 placeholder-gray-600 disabled:opacity-60"
		/>
		<button
			onclick={pickDirectory}
			disabled={lockedByEnv}
			class="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 transition-colors disabled:opacity-50"
		>
			Browse
		</button>
		<button
			onclick={saveConfig}
			disabled={lockedByEnv}
			class="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 transition-colors disabled:opacity-50"
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
		{#if lockedByEnv}
			<span class="text-xs text-yellow-500">Set by OBSIDIAN_VAULT_PATH in the environment</span>
		{/if}
	</div>

	<div class="flex items-center gap-2">
		<button
			onclick={handleExport}
			disabled={exporting || !vaultPath || !vaultExists}
			class="px-4 py-1.5 text-sm rounded bg-indigo-600 hover:bg-indigo-700 disabled:bg-gray-700 disabled:text-gray-500 text-white font-medium transition-colors"
		>
			{exporting ? 'Exporting...' : 'Export to Obsidian'}
		</button>
		<button onclick={loadPreview} class="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300">Preview</button>
		<button onclick={copyMarkdown} class="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300">Copy markdown</button>
	</div>
	<p class="text-xs text-gray-600">Tags, [[people]] links and transcript inclusion are configured in Settings.</p>

	{#if showPreview}
		<pre class="max-h-96 overflow-auto text-[11px] leading-4 text-gray-400 bg-gray-900 border border-gray-800 rounded p-3 whitespace-pre-wrap">{preview}</pre>
	{/if}

	{#if exportResult}
		<p class="text-green-400 text-sm">Exported to: <code class="text-green-300">{exportResult}</code></p>
	{/if}

	{#if error}
		<p class="text-red-400 text-sm">{error}</p>
	{/if}
</div>
