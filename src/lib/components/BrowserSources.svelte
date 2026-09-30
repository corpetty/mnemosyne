<script lang="ts">
	import { onMount } from 'svelte';
	import { listMicrophones } from '$lib/app/browser-capture.js';
	import { audioState, BROWSER_CALL, BROWSER_MIC } from '$lib/stores/audio.svelte.js';

	// What this browser records (a firm's server, app/browser-capture.ts): its microphone and the
	// call's audio, shared when recording starts.
	let mics = $state<{ id: string; label: string }[]>([]);
	const named = $derived(mics.some((m) => !/^Microphone \d+$/.test(m.label)));
	const isMac = typeof navigator !== 'undefined' && /Mac OS X/.test(navigator.userAgent);

	async function refresh() {
		mics = await listMicrophones();
	}

	/** Browsers name microphones only once one was allowed: ask once, then list again. */
	async function showNames() {
		try {
			const s = await navigator.mediaDevices.getUserMedia({ audio: true });
			for (const t of s.getTracks()) t.stop();
		} catch {
			/* refused: the default microphone still works */
		}
		await refresh();
	}

	onMount(() => {
		audioState.loadDevices();
		refresh();
	});
</script>

<section class="space-y-3" aria-label="What to record">
	<label class="flex items-start gap-2">
		<input
			type="checkbox"
			checked={audioState.selectedDeviceIds.has(BROWSER_MIC)}
			onchange={() => audioState.toggleDevice(BROWSER_MIC)}
			disabled={audioState.isRecording}
			class="mt-0.5 rounded border-gray-600 bg-gray-800"
		/>
		<span class="text-sm text-gray-300">
			Your microphone
			<span class="mt-1 flex items-center gap-2">
				<select
					value={audioState.browserMicId ?? ''}
					onchange={(e) => audioState.setBrowserMic(e.currentTarget.value || null)}
					disabled={audioState.isRecording || !audioState.selectedDeviceIds.has(BROWSER_MIC)}
					aria-label="Microphone"
					class="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200"
				>
					<option value="">Default microphone</option>
					{#each mics as m (m.id)}<option value={m.id}>{m.label}</option>{/each}
				</select>
				{#if !named}
					<button onclick={showNames} class="text-xs text-blue-400 hover:text-blue-300">Show their names</button>
				{/if}
			</span>
		</span>
	</label>
	<label class="flex items-start gap-2">
		<input
			type="checkbox"
			checked={audioState.selectedDeviceIds.has(BROWSER_CALL)}
			onchange={() => audioState.toggleDevice(BROWSER_CALL)}
			disabled={audioState.isRecording}
			class="mt-0.5 rounded border-gray-600 bg-gray-800"
		/>
		<span class="text-sm text-gray-300">
			The call's audio (Zoom, Teams, Meet)
			<span class="block text-xs text-gray-500">
				When recording starts, the browser asks what to share.
				{#if isMac}
					On a Mac, join the call in a browser tab, then choose that tab and tick "Share tab audio".
				{:else}
					Choose "Entire screen" and tick "Share system audio" for a call in its own app; for a call in a browser tab,
					choose that tab and tick "Share tab audio".
				{/if}
				Nothing of the screen is recorded, only the sound. Leave this off for a meeting in the room.
			</span>
		</span>
	</label>
</section>
