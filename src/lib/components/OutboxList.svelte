<script lang="ts">
	import { outboxAsNewMeeting, outboxDiscard } from '$lib/api/local.js';
	import { satelliteState } from '$lib/stores/satellite.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { OutboxEntry } from '$lib/types/index.js';

	// "n recordings waiting" (status bar): the recordings on this computer that have not reached
	// the main computer yet, and what to do with one it refused (backend services/satellite.py).
	let open = $state(false);
	let busy = $state<string | null>(null);

	const items = $derived(satelliteState.waiting);
	const failed = $derived(items.some((e) => e.state === 'failed'));
	const main = $derived(satelliteState.mainName);

	const when = (iso: string) =>
		new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });

	function stateText(e: OutboxEntry): string {
		if (e.state === 'sending') return `Being sent to ${main}`;
		if (e.state === 'failed') return `${main} did not take it`;
		return `Waiting for ${main}`;
	}

	async function newMeeting(e: OutboxEntry) {
		busy = e.id;
		try {
			await outboxAsNewMeeting(e.id);
			toastState.info(`It will go to ${main} as a new meeting`);
			await satelliteState.refresh();
		} catch (err) {
			toastState.error(err instanceof Error ? err.message : 'Could not send it as a new meeting');
		} finally {
			busy = null;
		}
	}

	async function discard(e: OutboxEntry) {
		const name = e.title || 'this recording';
		if (!confirm(`Delete ${name} from this computer for good? Its audio has not reached ${main}, so it cannot be recovered.`)) return;
		busy = e.id;
		try {
			await outboxDiscard(e.id);
			await satelliteState.refresh();
		} catch (err) {
			toastState.error(err instanceof Error ? err.message : 'Could not discard it');
		} finally {
			busy = null;
		}
	}
</script>

{#if items.length}
	<div class="relative">
		<button
			onclick={() => (open = !open)}
			aria-expanded={open}
			title="Recordings kept on this computer"
			class="hover:text-gray-300 {failed ? 'text-amber-400' : 'text-gray-400'}"
		>{items.length} recording{items.length === 1 ? '' : 's'} waiting</button>
		{#if open}
			<div
				class="absolute bottom-full right-0 mb-2 w-96 max-w-[90vw] rounded-lg border border-gray-700 bg-gray-900 p-2 shadow-lg z-20 space-y-2"
				role="region"
				aria-label="Recordings waiting"
			>
				<ul class="divide-y divide-gray-800">
					{#each items as e (e.id)}
						<li class="py-1.5 space-y-0.5">
							<div class="flex items-baseline justify-between gap-2">
								<span class="text-sm text-gray-200 truncate">{e.title || 'Untitled recording'}</span>
								<span class="text-[11px] text-gray-500 whitespace-nowrap">{when(e.started_at)}</span>
							</div>
							<p class="text-xs {e.state === 'failed' ? 'text-amber-300' : 'text-gray-400'}">{stateText(e)}{e.parts > 1 ? ` · ${e.parts} parts` : ''}</p>
							{#if e.state === 'failed' && e.last_error}
								<p class="text-xs text-amber-200/80">{e.last_error}</p>
							{/if}
							{#if e.state === 'failed'}
								<div class="flex items-center gap-3 pt-0.5">
									<button
										onclick={() => newMeeting(e)}
										disabled={busy === e.id}
										class="rounded bg-gray-800 border border-gray-700 px-2 py-0.5 text-xs text-gray-200 hover:bg-gray-700 disabled:opacity-50"
									>Send as a new meeting</button>
									<button
										onclick={() => discard(e)}
										disabled={busy === e.id}
										class="text-xs text-red-400 hover:text-red-300 disabled:opacity-50"
									>Discard</button>
								</div>
							{/if}
						</li>
					{/each}
				</ul>
			</div>
		{/if}
	</div>
{/if}
