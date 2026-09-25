<script lang="ts">
	import { toggleView } from '$lib/app/controller.svelte.js';
	import { connectionState } from '$lib/stores/connection.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { uiState, type View } from '$lib/stores/ui.svelte.js';
	import { wsState } from '$lib/stores/websocket.svelte.js';

	const NAV: { view: View; label: string; title: string }[] = [
		{ view: 'ask', label: 'Ask', title: 'Ask across all meetings (Ctrl+K)' },
		{ view: 'tasks', label: 'Tasks', title: 'Action items from all meetings' },
		{ view: 'people', label: 'People', title: 'People across your meetings' },
		{ view: 'topics', label: 'Topics', title: 'Follow a topic across meetings' },
		{ view: 'digest', label: 'Digest', title: 'Digest of a week of meetings' }
	];

	const active = (v: View) => uiState.view === v && !sessionState.activeSession;
	const status = $derived(
		uiState.backendStatus !== 'connected'
			? uiState.backendStatus
			: connectionState.isLocal
				? wsState.connected
					? 'Connected'
					: 'Connected (no live updates)'
				: `Connected to ${connectionState.host ?? connectionState.url}`
	);
</script>

<header class="border-b border-gray-800 px-4 py-2 flex items-center gap-4 flex-shrink-0">
	<div class="flex items-center gap-3">
		<button
			onclick={() => (uiState.sidebarCollapsed = !uiState.sidebarCollapsed)}
			class="p-1 rounded hover:bg-gray-800 text-gray-400 hover:text-gray-200 transition-colors"
			title="Toggle sidebar (Ctrl+B)"
			aria-label="Toggle sidebar"
		>
			<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" />
			</svg>
		</button>
		<button onclick={() => toggleView('home')} class="text-lg font-bold tracking-tight hover:text-white" title="Home">
			Mnemosyne
		</button>
	</div>

	<nav class="flex items-center rounded-lg bg-gray-900 border border-gray-800 p-0.5" aria-label="Views">
		{#each NAV as item (item.view)}
			<button
				onclick={() => toggleView(item.view)}
				title={item.title}
				aria-current={active(item.view) ? 'page' : undefined}
				class="px-3 py-1 rounded-md text-xs font-medium transition-colors
					{active(item.view) ? 'bg-gray-700 text-gray-100' : 'text-gray-400 hover:text-gray-200'}"
			>
				{item.label}
			</button>
		{/each}
	</nav>

	<div class="ml-auto flex items-center gap-3 text-xs">
		<span class="flex items-center gap-1.5 text-gray-500" title={status}>
			<span
				class="w-2 h-2 rounded-full {uiState.backendStatus === 'connected'
					? wsState.connected
						? 'bg-green-500'
						: 'bg-yellow-500'
					: uiState.backendStatus === 'checking'
						? 'bg-yellow-500 animate-pulse'
						: 'bg-red-500'}"
			></span>
			<span class="hidden sm:inline">{status}</span>
		</span>
		<button
			onclick={() => toggleView('settings')}
			aria-label="Settings"
			title="Settings"
			aria-current={active('settings') ? 'page' : undefined}
			class="p-1.5 rounded transition-colors {active('settings') ? 'bg-gray-700 text-gray-100' : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800'}"
		>
			<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.3 4.3c.4-1.7 3-1.7 3.4 0a1.7 1.7 0 0 0 2.6 1.1c1.5-.9 3.3.8 2.4 2.4a1.7 1.7 0 0 0 1 2.5c1.8.4 1.8 3 0 3.4a1.7 1.7 0 0 0-1 2.6c.9 1.5-.9 3.3-2.4 2.4a1.7 1.7 0 0 0-2.6 1c-.4 1.8-3 1.8-3.4 0a1.7 1.7 0 0 0-2.5-1c-1.6.9-3.3-.9-2.4-2.4a1.7 1.7 0 0 0-1.1-2.6c-1.7-.4-1.7-3 0-3.4a1.7 1.7 0 0 0 1.1-2.5c-.9-1.6.8-3.3 2.4-2.4a1.7 1.7 0 0 0 2.5-1.1z" />
				<circle cx="12" cy="12" r="3" stroke-width="2" />
			</svg>
		</button>
	</div>
</header>
