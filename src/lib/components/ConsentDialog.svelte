<script lang="ts">
	import { onMount } from 'svelte';
	import { getSettings } from '$lib/api/backend.js';
	import { focusOnMount } from '$lib/app/focus.js';
	import { startRecording, type Consent } from '$lib/app/controller.svelte.js';
	import { uiState } from '$lib/stores/ui.svelte.js';

	// Before a recording, when the server asks for it (require_consent, firm mode): how did the
	// people in this meeting agree? The answer goes into the meeting's history.
	const OPTIONS: [Consent, string, string][] = [
		['all_parties', 'Everyone was told and agreed', 'A call or meeting where each person heard it will be recorded and said yes.'],
		['in_person', 'In the room: everyone was told', 'A meeting in person, with everyone present told it is being recorded.'],
		['one_party', 'Only I agreed (one-party consent)', 'Only where the law allows recording with one party’s consent.']
	];
	let choice = $state<Consent | null>(null);
	let script = $state('');

	onMount(async () => {
		try {
			script = (await getSettings()).values.consent_script ?? '';
		} catch {
			/* the choice still works without the script */
		}
	});

	const cancel = () => (uiState.consentAsk = null);

	function start() {
		const ask = uiState.consentAsk;
		if (!ask || !choice) return;
		uiState.consentAsk = null;
		// Straight from this click: the browser's microphone and share dialogs need it.
		void startRecording(ask.fresh, choice);
	}
</script>

<div class="fixed inset-0 z-50 flex items-start justify-center bg-black/50 px-4 pt-[15vh]" role="presentation" onclick={cancel}>
	<div
		class="w-full max-w-md space-y-3 rounded-lg border border-gray-700 bg-gray-900 p-4 shadow-2xl"
		role="dialog"
		aria-modal="true"
		aria-label="Consent to record"
		tabindex="-1"
		use:focusOnMount
		onclick={(e) => e.stopPropagation()}
		onkeydown={(e) => e.key === 'Escape' && cancel()}
	>
		<h3 class="text-lg font-semibold text-gray-100">Before recording</h3>
		<p class="text-sm text-gray-400">How did the people in this meeting agree to be recorded?</p>
		{#if script}
			<blockquote class="rounded border border-gray-800 bg-gray-950 px-3 py-2 text-sm text-gray-300">
				<span class="block text-xs text-gray-500 mb-1">You can read this out:</span>
				{script}
			</blockquote>
		{/if}
		<div class="space-y-1.5" role="radiogroup" aria-label="Consent">
			{#each OPTIONS as [value, label, hint] (value)}
				<label class="flex items-start gap-2 rounded border px-3 py-2 cursor-pointer {choice === value ? 'border-blue-600 bg-blue-950/30' : 'border-gray-800'}">
					<input type="radio" name="consent" {value} bind:group={choice} class="mt-1" />
					<span>
						<span class="block text-sm text-gray-200">{label}</span>
						<span class="block text-xs text-gray-500">{hint}</span>
					</span>
				</label>
			{/each}
		</div>
		<div class="flex items-center gap-3">
			<button onclick={start} disabled={!choice} class="rounded bg-red-600 px-3 py-1.5 text-sm text-white hover:bg-red-500 disabled:opacity-50">
				Start recording
			</button>
			<button onclick={cancel} class="text-sm text-gray-400 hover:text-gray-200">Cancel</button>
		</div>
		<p class="text-xs text-gray-500">Your answer, and who gave it, is kept in the meeting's history.</p>
	</div>
</div>
