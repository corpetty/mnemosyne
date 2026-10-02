<script lang="ts">
	import { onMount } from 'svelte';
	import { getSystemInfo, updateSettings } from '$lib/api/backend.js';
	import { uiState } from '$lib/stores/ui.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { SystemInfo } from '$lib/types/index.js';

	// GPU support (the gpu extra: torch, WhisperX, NeMo) on an NVIDIA machine where it is not
	// installed: the desktop app installs it in the background (src-tauri/src/lib.rs
	// start_gpu_install), unless setup or this card said not now (gpu_support = "off").
	let system = $state<SystemInfo | null>(null);
	let desktop = $state(false);

	onMount(async () => {
		desktop = (await import('@tauri-apps/api/core')).isTauri();
		if (desktop) system = await getSystemInfo().catch(() => null);
	});

	const shown = $derived(desktop && !!system?.gpu_driver && !system.gpu_stack);

	async function install() {
		try {
			await updateSettings({ gpu_support: 'auto' });
			const { invoke } = await import('@tauri-apps/api/core');
			const started = await invoke<boolean>('start_gpu_install');
			if (system) system.gpu_support = 'auto';
			if (!started && !uiState.gpuInstall) toastState.info('GPU support installs the next time Mnemosyne starts');
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not start the install');
		}
	}
</script>

{#if shown && system}
	<section aria-label="GPU support" class="rounded border border-gray-800 bg-gray-900 p-3 text-sm space-y-2">
		<h3 class="font-semibold text-gray-200">GPU support</h3>
		<p class="text-xs text-gray-400">
			This computer has an NVIDIA GPU. GPU support adds WhisperX (many languages) and Nemotron (better, faster speaker
			labels): about 7 GB, downloaded in the background while Parakeet keeps working.
		</p>
		{#if uiState.gpuInstall?.state === 'installing'}
			<p class="text-xs text-gray-500">Installing… (progress in the status bar)</p>
		{:else if system.gpu_support === 'off'}
			<button onclick={install} class="px-3 py-1.5 rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200">Install GPU support</button>
		{:else}
			<p class="text-xs text-gray-500">It installs in the background; Mnemosyne restarts its engine when it is done.</p>
		{/if}
	</section>
{/if}
