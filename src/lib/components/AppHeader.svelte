<script lang="ts">
	import { toggleAsk, toggleDigest, toggleSettings } from '$lib/app/controller.svelte.js';
	import { connectionState } from '$lib/stores/connection.svelte.js';
	import { uiState } from '$lib/stores/ui.svelte.js';
	import { wsState } from '$lib/stores/websocket.svelte.js';
</script>

<header class="border-b border-gray-800 px-4 py-2 flex items-center justify-between flex-shrink-0">
	<div class="flex items-center gap-3">
		<button
			onclick={() => (uiState.sidebarCollapsed = !uiState.sidebarCollapsed)}
			class="p-1 rounded hover:bg-gray-800 text-gray-400 hover:text-gray-200 transition-colors"
			title="Toggle sidebar (Ctrl+B)"
		>
			<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" />
			</svg>
		</button>
		<h1 class="text-lg font-bold tracking-tight">Mnemosyne</h1>
	</div>
	<div class="flex items-center gap-3 text-sm">
		<div class="flex items-center gap-1.5">
			<span class="w-2 h-2 rounded-full {uiState.backendStatus === 'connected' ? 'bg-green-500' : uiState.backendStatus === 'checking' ? 'bg-yellow-500 animate-pulse' : 'bg-red-500'}"></span>
			<span class="text-gray-500">{uiState.backendStatus === 'connected' ? (connectionState.isLocal ? 'API' : `API @ ${connectionState.host ?? connectionState.url}`) : uiState.backendStatus}</span>
		</div>
		{#if wsState.connected}
			<div class="flex items-center gap-1.5">
				<span class="w-2 h-2 rounded-full bg-blue-500"></span>
				<span class="text-gray-500">WS</span>
			</div>
		{/if}
		<button
			onclick={toggleAsk}
			title="Ask across all meetings (Ctrl+K)"
			class="px-2.5 py-1 rounded text-xs font-medium transition-colors
				{uiState.showAsk ? 'bg-gray-700 text-gray-200' : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800'}"
		>
			Ask
		</button>
		<button
			onclick={toggleDigest}
			title="Digest of a week of meetings"
			class="px-2.5 py-1 rounded text-xs font-medium transition-colors
				{uiState.showDigest ? 'bg-gray-700 text-gray-200' : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800'}"
		>
			Digest
		</button>
		<button
			onclick={toggleSettings}
			class="px-2.5 py-1 rounded text-xs font-medium transition-colors
				{uiState.showSettings ? 'bg-gray-700 text-gray-200' : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800'}"
		>
			Settings
		</button>
	</div>
</header>
