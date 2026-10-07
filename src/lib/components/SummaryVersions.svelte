<script lang="ts">
	// Earlier summaries of a meeting (backend storage keep_version: kept whenever the summary is
	// replaced, edited or restored), with the text shown on demand and a way back to one.
	import { getRecordVersion, listVersions, restoreSummaryVersion } from '$lib/api/backend.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { RecordVersion, VersionInfo } from '$lib/types/index.js';
	import Markdown from './Markdown.svelte';

	let { sessionId, canRestore, onclose }: { sessionId: string; canRestore: boolean; onclose: () => void } =
		$props();

	let versions = $state<VersionInfo[] | null>(null);
	let shown = $state<RecordVersion | null>(null);
	let restoring = $state(false);

	$effect(() => {
		listVersions(sessionId)
			.then((v) => (versions = v.filter((x) => x.has_summary).reverse()))
			.catch((e) => toastState.error(e instanceof Error ? e.message : 'Could not list the versions'));
	});

	async function show(id: number) {
		try {
			shown = shown?.id === id ? null : await getRecordVersion(sessionId, id);
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not load that version');
		}
	}

	async function restore(v: VersionInfo) {
		if (!confirm('Put this earlier summary back? The current one is kept as a version.')) return;
		restoring = true;
		try {
			await restoreSummaryVersion(sessionId, v.id);
			await sessionState.refreshActive();
			toastState.success('Earlier summary restored');
			onclose();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not restore it');
		} finally {
			restoring = false;
		}
	}

	const when = (iso: string) => new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
</script>

<section class="bg-gray-900 border border-gray-700 rounded-lg p-3 text-sm" aria-label="Earlier summaries">
	<div class="flex items-center mb-2">
		<h4 class="text-xs font-semibold text-gray-400 uppercase tracking-wide mr-auto">Earlier summaries</h4>
		<button onclick={onclose} class="text-xs text-gray-500 hover:text-gray-200" aria-label="Close earlier summaries">✕</button>
	</div>
	{#if versions === null}
		<p class="text-xs text-gray-500">Loading…</p>
	{:else if versions.length === 0}
		<p class="text-xs text-gray-500">No earlier summaries: one is kept each time the summary is replaced, edited or restored.</p>
	{:else}
		<ul class="space-y-1">
			{#each versions as v (v.id)}
				<li>
					<div class="flex items-center gap-2">
						<button onclick={() => show(v.id)} class="text-left text-gray-300 hover:text-white">
							{when(v.at)} <span class="text-gray-500">· replaced when {v.reason}{v.by ? ` by ${v.by}` : ''}</span>
						</button>
						{#if canRestore}
							<button
								onclick={() => restore(v)}
								disabled={restoring}
								class="ml-auto shrink-0 px-2 py-0.5 text-xs rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200 disabled:opacity-50"
							>Restore</button>
						{/if}
					</div>
					{#if shown?.id === v.id}
						<div class="mt-1 max-h-72 overflow-y-auto rounded border border-gray-800 bg-gray-950 p-2 text-gray-300">
							{#if shown.summary_data?.topics.length}
								<p class="text-[11px] text-gray-500 mb-1">{shown.summary_data.topics.join(' · ')}</p>
							{/if}
							<Markdown text={shown.summary} />
						</div>
					{/if}
				</li>
			{/each}
		</ul>
	{/if}
</section>
