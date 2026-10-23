<script lang="ts">
	import { copyText } from '$lib/app/clipboard.js';
	import { shortDate, whereLabel } from '$lib/app/publishing.js';
	import { publishingState } from '$lib/stores/publishing.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { uiState } from '$lib/stores/ui.svelte.js';

	// This library's live publications (GET /api/publications), in Settings → Publishing.
	$effect(() => {
		void publishingState.loadList();
	});

	async function openMeeting(id: string) {
		await sessionState.selectSession(id);
		uiState.view = 'home';
	}
</script>

<div>
	<h4 class="mb-1.5 text-sm font-medium text-gray-300">Published meetings</h4>
	{#if publishingState.list === null}
		<p class="text-xs text-gray-500">Loading…</p>
	{:else if publishingState.list.length === 0}
		<p class="text-xs text-gray-500">Nothing is published. Publish a meeting from its ⋯ menu.</p>
	{:else}
		<ul class="divide-y divide-gray-800 rounded border border-gray-800 text-sm" aria-label="Published meetings">
			{#each publishingState.list as item (item.publication.id)}
				{@const p = item.publication}
				<li class="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2">
					<span class="min-w-0 flex-1 truncate text-gray-200">{item.session_name}</span>
					<span class="text-xs text-gray-500">
						{whereLabel(p.where, p.host_url)} · v{p.version} · {p.expires_at ? `expires ${shortDate(p.expires_at)}` : 'no expiry'}
					</span>
					{#if p.url}
						<button onclick={() => copyText(p.url ?? '', 'Link copied')} class="text-xs text-gray-400 hover:text-gray-200">Copy link</button>
					{/if}
					<button onclick={() => openMeeting(p.session_id)} class="text-xs text-blue-400 hover:text-blue-300">Open meeting</button>
				</li>
			{/each}
		</ul>
	{/if}
</div>
