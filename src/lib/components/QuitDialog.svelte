<script lang="ts">
	import { keepRecordingInBackground, stopSaveAndQuit } from '$lib/app/controller.svelte.js';
	import { uiState } from '$lib/stores/ui.svelte.js';

	// Closing the window or choosing Quit in the tray during a recording asks here first
	// (the shell holds the quit back: src-tauri/src/lib.rs quit_or_ask).
	const cancel = () => {
		if (!uiState.quitSaving) uiState.quitAsk = false;
	};
</script>

<div class="fixed inset-0 z-50 flex items-start justify-center bg-black/50 px-4 pt-[20vh]" role="presentation" onclick={cancel}>
	<div
		class="w-full max-w-md space-y-3 rounded-lg border border-gray-700 bg-gray-900 p-4 shadow-2xl"
		role="dialog"
		aria-modal="true"
		aria-label="Quit while recording"
		tabindex="-1"
		onclick={(e) => e.stopPropagation()}
		onkeydown={(e) => e.key === 'Escape' && cancel()}
	>
		<h3 class="text-lg font-semibold text-gray-100">A recording is running</h3>
		<p class="text-sm text-gray-400">Quitting now would cut it off. What would you like to do?</p>
		<div class="flex flex-col gap-2">
			<button
				onclick={stopSaveAndQuit}
				disabled={uiState.quitSaving}
				class="rounded bg-red-700 px-3 py-2 text-sm text-white hover:bg-red-600 disabled:opacity-60"
			>
				{uiState.quitSaving ? 'Saving the recording…' : 'Stop, save and quit'}
			</button>
			<button
				onclick={keepRecordingInBackground}
				disabled={uiState.quitSaving}
				class="rounded bg-gray-800 px-3 py-2 text-sm text-gray-200 hover:bg-gray-700 disabled:opacity-60"
			>
				Keep recording in the background
			</button>
			<button onclick={cancel} disabled={uiState.quitSaving} class="px-3 py-1 text-sm text-gray-400 hover:text-gray-200">
				Cancel
			</button>
		</div>
		<p class="text-xs text-gray-500">A recording saved on quit is transcribed when you ask for it.</p>
	</div>
</div>
