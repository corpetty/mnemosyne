<script lang="ts">
	import { onMount } from 'svelte';

	// The desktop window (src-tauri/src/shell_prefs.rs): kept by the app, not the backend.
	let prefs = $state<{ close_to_tray: boolean; tray: boolean } | null>(null);

	async function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T | null> {
		const core = await import('@tauri-apps/api/core');
		return core.isTauri() ? core.invoke<T>(cmd, args) : null;
	}

	onMount(() => {
		invoke<{ close_to_tray: boolean; tray: boolean }>('shell_prefs')
			.then((p) => (prefs = p))
			.catch(() => (prefs = null));
	});

	async function setCloseToTray(enabled: boolean) {
		await invoke('set_close_to_tray', { enabled });
		if (prefs) prefs.close_to_tray = enabled;
	}
</script>

{#if prefs}
	<section aria-label="Window">
		<h3 class="text-lg font-semibold text-gray-200 mb-1">Window</h3>
		<label class="flex items-center gap-2 text-sm text-gray-300">
			<input
				type="checkbox"
				checked={prefs.close_to_tray}
				disabled={!prefs.tray}
				onchange={(e) => setCloseToTray(e.currentTarget.checked)}
			/>
			Closing the window keeps Mnemosyne running in the tray
		</label>
		<p class="text-xs text-gray-500 mt-1 ml-6">
			{#if prefs.tray}
				Recordings and sharing go on; Show in the tray brings the window back, Quit there quits.
			{:else}
				There is no system tray here. On GNOME, install the AppIndicator extension, then restart Mnemosyne.
			{/if}
		</p>
	</section>
{/if}
