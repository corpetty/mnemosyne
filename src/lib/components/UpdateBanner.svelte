<script lang="ts">
	import { audioState } from '$lib/stores/audio.svelte.js';
	import { updateState } from '$lib/stores/update.svelte.js';

	const pct = $derived(updateState.total ? Math.round((100 * updateState.downloaded) / updateState.total) : null);
</script>

{#if !updateState.dismissed && (updateState.status === 'available' || updateState.status === 'downloading' || updateState.status === 'restarting')}
	<div class="flex items-center justify-between gap-3 border-b border-emerald-900 bg-emerald-950/40 px-4 py-2 text-sm flex-shrink-0">
		<span class="text-emerald-100">
			{#if updateState.status === 'downloading'}
				Downloading Mnemosyne {updateState.version}{pct !== null ? ` · ${pct}%` : '…'}
			{:else if updateState.status === 'restarting'}
				Installed. Restarting…
			{:else}
				Mnemosyne <span class="font-medium">{updateState.version}</span> is available
				<span class="text-emerald-300/70">(you have {updateState.current})</span>
			{/if}
		</span>
		{#if updateState.status === 'available'}
			<div class="flex items-center gap-2">
				<button
					onclick={() => updateState.install()}
					disabled={audioState.isRecording}
					title={audioState.isRecording ? 'Stop recording first' : 'Download, install and restart'}
					class="px-3 py-1 rounded bg-emerald-600 hover:bg-emerald-700 disabled:bg-gray-700 disabled:text-gray-400 text-white text-xs font-medium"
				>
					Update and restart
				</button>
				<button onclick={() => (updateState.dismissed = true)} class="text-xs text-emerald-300 hover:text-emerald-100">Later</button>
			</div>
		{/if}
	</div>
{/if}
