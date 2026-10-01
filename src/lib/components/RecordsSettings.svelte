<script lang="ts">
	import { getDeletions } from '$lib/api/backend.js';
	import { exportRecords } from '$lib/app/records.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { Deletion } from '$lib/types/index.js';

	// Records across meetings (backend services/records.py): an export by date,
	// and what was deleted, by whom and why. The records period itself is a setting above.
	const today = new Date().toISOString().slice(0, 10);
	let start = $state(new Date(Date.now() - 90 * 86400_000).toISOString().slice(0, 10));
	let end = $state(today);
	let exporting = $state(false);
	let deletions = $state<Deletion[] | null>(null);

	async function exportRange() {
		exporting = true;
		try {
			const n = await exportRecords({ start, end });
			toastState.info(`Export of ${n} meeting${n === 1 ? '' : 's'} ready: downloading`);
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not export');
		} finally {
			exporting = false;
		}
	}

	async function showDeletions() {
		try {
			deletions = await getDeletions();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not load the deletion log');
		}
	}
</script>

<section aria-label="Records">
	<h3 class="text-lg font-semibold text-gray-200 mb-1">Records</h3>
	<p class="text-xs text-gray-500 mb-3">
		Each meeting's transcript and summary are sealed (a chained fingerprint, checked on its Record card), earlier versions
		are kept, and deletions are logged. An export holds the meetings' audio, transcripts, summaries, versions,
		history and seals, with checksums; it can be downloaded once.
	</p>
	<div class="flex flex-wrap items-end gap-2 mb-3">
		<label class="text-xs text-gray-400">Meetings from<input type="date" bind:value={start} max={end} class="block mt-1 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-100" /></label>
		<label class="text-xs text-gray-400">to<input type="date" bind:value={end} min={start} max={today} class="block mt-1 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-100" /></label>
		<button onclick={exportRange} disabled={exporting || !start} class="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200 disabled:opacity-50">
			{exporting ? 'Preparing…' : 'Export records'}
		</button>
	</div>
	{#if deletions === null}
		<button onclick={showDeletions} class="text-sm text-blue-400 hover:text-blue-300">Show the deletion log</button>
	{:else if deletions.length === 0}
		<p class="text-sm text-gray-500">Nothing has been deleted.</p>
	{:else}
		<table class="w-full text-xs">
			<thead class="text-left text-gray-500"><tr><th class="font-normal py-1">When</th><th class="font-normal">What</th><th class="font-normal">By</th><th class="font-normal">Why</th></tr></thead>
			<tbody>
				{#each [...deletions].reverse() as d (d.at + d.session_id + d.what)}
					<tr class="border-t border-gray-800 text-gray-300">
						<td class="py-1">{new Date(d.at).toLocaleString()}</td>
						<td>{d.what === 'audio' ? 'Audio of' : 'Meeting'} “{d.name}” ({new Date(d.created_at).toLocaleDateString()})</td>
						<td>{d.by || 'admin'}{d.role && d.role !== 'admin' ? ` (${d.role})` : ''}</td>
						<td>{d.reason || '—'}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}
</section>
