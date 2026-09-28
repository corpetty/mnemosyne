<script lang="ts">
	import { addLink, assetFileUrl, attachAsset, detachAsset, listAssets, uploadAsset } from '$lib/api/backend.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { Asset, LibraryAsset } from '$lib/types/index.js';

	// Links and files for this meeting. They come from a library shared by every meeting, so
	// what was shared in an earlier one can be attached again. The summary reads them.
	let { compact = false }: { compact?: boolean } = $props();

	const session = $derived(sessionState.activeSession);
	const items = $derived(session?.assets ?? []);
	let url = $state('');
	let busy = $state(false);
	let picking = $state(false);
	let query = $state('');
	let library = $state<LibraryAsset[]>([]);

	async function refresh() {
		await sessionState.refreshActive();
	}

	async function run(op: () => Promise<unknown>, ok?: string) {
		busy = true;
		try {
			await op();
			await refresh();
			if (ok) toastState.success(ok);
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Something went wrong');
		} finally {
			busy = false;
		}
	}

	function addUrl() {
		const u = url.trim();
		if (!u || !session) return;
		url = '';
		run(() => addLink(u, '', session.id));
	}

	function chooseFile() {
		const input = document.createElement('input');
		input.type = 'file';
		input.onchange = () => {
			const f = input.files?.[0];
			if (f && session) run(() => uploadAsset(f, session.id), `Added ${f.name}`);
		};
		input.click();
	}

	async function open(asset: Asset) {
		const href = asset.kind === 'link' && asset.url ? asset.url : assetFileUrl(asset.id);
		try {
			const { invoke } = await import('@tauri-apps/api/core');
			await invoke('plugin:shell|open', { path: href });
		} catch {
			window.open(href, '_blank', 'noopener,noreferrer');
		}
	}

	async function search() {
		try {
			library = await listAssets(query.trim());
		} catch {
			library = [];
		}
	}

	$effect(() => {
		if (picking) search();
	});

	const attached = $derived(new Set(items.map((a) => a.id)));
</script>

{#if session && (items.length || !compact)}
	<section class={compact ? 'space-y-1.5' : 'rounded-lg border border-gray-800 bg-gray-900/40 p-3 space-y-2'} aria-label="Resources">
		<header class="flex items-baseline gap-2">
			<h4 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500">Resources</h4>
			{#if items.length}<span class="text-xs text-gray-500">{items.length}</span>{/if}
			<button onclick={() => (picking = true)} class="ml-auto text-xs text-blue-400 hover:text-blue-300">From earlier meetings…</button>
		</header>
		{#if items.length}
			<ul class="space-y-1 text-sm">
				{#each items as asset (asset.id)}
					<li class="group flex items-center gap-2">
						<span aria-hidden="true">{asset.kind === 'link' ? '🔗' : '📄'}</span>
						<button onclick={() => open(asset)} class="truncate text-left text-gray-200 hover:text-white hover:underline" title={asset.url ?? asset.filename ?? ''}>
							{asset.title}
						</button>
						{#if asset.source === 'calendar'}<span class="text-[10px] text-gray-500">from the invite</span>{/if}
						<button
							onclick={() => run(() => detachAsset(session.id, asset.id))}
							disabled={busy}
							class="ml-auto opacity-0 group-hover:opacity-100 px-1 text-gray-500 hover:text-gray-200"
							aria-label={`Take ${asset.title} off this meeting`}
						>✕</button>
					</li>
				{/each}
			</ul>
		{:else}
			<p class="text-xs text-gray-500">Docs, decks and tickets for this meeting. The summary reads them for context and names.</p>
		{/if}
		<div class="flex gap-2">
			<input
				bind:value={url}
				onkeydown={(e) => e.key === 'Enter' && addUrl()}
				disabled={busy}
				placeholder="Paste a link, then Enter"
				class="min-w-0 flex-1 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-200"
			/>
			<button onclick={chooseFile} disabled={busy} class="px-2 py-1 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300">Add a file</button>
		</div>
	</section>
{/if}

<svelte:window onkeydown={(e) => picking && e.key === 'Escape' && (picking = false)} />

{#if picking && session}
	<div class="fixed inset-0 z-40 flex items-start justify-center bg-black/50 px-4 pt-[12vh]" role="presentation" onclick={() => (picking = false)}>
		<div
			class="w-full max-w-lg space-y-3 rounded-lg border border-gray-700 bg-gray-900 p-4 shadow-2xl"
			role="dialog"
			aria-modal="true"
			aria-label="Resources from earlier meetings"
			tabindex="-1"
			onclick={(e) => e.stopPropagation()}
			onkeydown={() => {}}
		>
			<h3 class="text-lg font-semibold text-gray-100">Resources from earlier meetings</h3>
			<input bind:value={query} oninput={search} placeholder="Find a resource" class="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200" />
			<ul class="max-h-80 overflow-y-auto divide-y divide-gray-800" aria-label="Library">
				{#each library as item (item.asset.id)}
					<li class="flex items-center gap-2 px-1 py-2 text-sm">
						<span aria-hidden="true">{item.asset.kind === 'link' ? '🔗' : '📄'}</span>
						<span class="truncate text-gray-200">{item.asset.title}</span>
						<span class="shrink-0 text-xs text-gray-500">{item.used} meeting{item.used === 1 ? '' : 's'}</span>
						{#if attached.has(item.asset.id)}
							<span class="ml-auto text-xs text-gray-500">attached</span>
						{:else}
							<button onclick={() => run(() => attachAsset(session.id, item.asset.id), 'Attached')} class="ml-auto text-xs text-blue-400 hover:text-blue-300">Attach</button>
						{/if}
					</li>
				{:else}
					<li class="px-1 py-2 text-sm text-gray-500">Nothing yet: resources you add to meetings collect here.</li>
				{/each}
			</ul>
		</div>
	</div>
{/if}
