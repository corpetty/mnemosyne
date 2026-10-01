<script lang="ts">
	import { uiState } from '$lib/stores/ui.svelte.js';
	import { connectionState } from '$lib/stores/connection.svelte.js';
</script>

<div class="flex-1 flex items-center justify-center px-4">
	<section class="w-full max-w-md space-y-3 rounded-lg border border-gray-800 bg-gray-900 p-5" aria-label="Sign in">
		<h2 class="text-lg font-semibold text-gray-100">Sign in to Mnemosyne</h2>
		<p class="text-sm text-gray-400">
			This is your team's Mnemosyne{connectionState.host ? ` on ${connectionState.host}` : ''}. To sign in, open the
			invite link your administrator sent you, in this browser.
		</p>
		<p class="text-sm text-gray-500">
			Each link works once, for one browser. For another computer or browser, or if your link has expired, ask for a
			new one.
		</p>
		{#if uiState.signInError}
			<p class="text-sm text-red-400">{uiState.signInError}</p>
		{/if}
	</section>
</div>
