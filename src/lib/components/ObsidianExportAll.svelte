<script lang="ts">
	import { exportAllToObsidian, getJob } from '$lib/api/backend.js';
	import { toastState } from '$lib/stores/toast.svelte.js';

	// Writes every summarized meeting to the vault again (backend export route `obsidian_export_all`).
	let busy = $state(false);
	let message = $state('');

	async function run() {
		busy = true;
		message = 'Starting…';
		try {
			const job = await exportAllToObsidian();
			for (;;) {
				const j = await getJob(job.id);
				message = j.message || 'Exporting…';
				if (j.status === 'completed') {
					const r = (j.result ?? {}) as { exported?: number; failed?: number };
					const failed = r.failed ? `, ${r.failed} failed` : '';
					toastState.success(`Exported ${r.exported ?? 0} meetings${failed}`);
					break;
				}
				if (j.status === 'failed' || j.status === 'cancelled') {
					toastState.error(j.error ?? 'The export failed');
					break;
				}
				await new Promise((r) => setTimeout(r, 700));
			}
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not export');
		} finally {
			busy = false;
			message = '';
		}
	}
</script>

<div class="col-span-2 flex items-center gap-3">
	<button onclick={run} disabled={busy} class="px-3 py-1.5 text-sm rounded bg-gray-700 hover:bg-gray-600 text-white disabled:opacity-50">
		Export all meetings again
	</button>
	<span class="text-[11px] text-gray-500">
		{message || 'Rewrites the notes Mnemosyne exported, e.g. after changing how tasks are written.'}
	</span>
</div>
