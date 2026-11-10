<script lang="ts">
	// Beside a "make a pairing code / invite" button: how long the next one works.
	import { CODE_LENGTHS, codeLength } from '$lib/app/codeLength.svelte.js';
</script>

<label class="flex items-center gap-1.5 text-xs text-gray-500">
	works for
	<select
		bind:value={codeLength.minutes}
		class="bg-gray-800 border border-gray-700 rounded px-1.5 py-1 text-xs text-gray-200"
		title="How long a new code or invite works. Each works once; restarting the app ends unused ones."
	>
		{#each CODE_LENGTHS as l (l.minutes)}<option value={l.minutes}>{l.label}</option>{/each}
	</select>
</label>
