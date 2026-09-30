<script lang="ts">
	import { getSupervisionQueue, scanForCompliancePhrases } from '$lib/api/backend.js';
	import { connectionState } from '$lib/stores/connection.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { wsState } from '$lib/stores/websocket.svelte.js';
	import type { Job, SupervisionQueueItem } from '$lib/types/index.js';

	// Meetings with lines that use a compliance phrase (backend services/supervision.py), the
	// ones still to review first. Opening one shows its flags above the transcript.
	let { onOpenSession }: { onOpenSession?: () => void } = $props();

	let items = $state<SupervisionQueueItem[]>([]);
	let loaded = $state(false);
	let show = $state<'open' | 'reviewed' | 'all'>('open');
	let scanning = $state(false);
	const isAdmin = $derived(connectionState.me?.role === 'admin');

	async function load() {
		try {
			items = await getSupervisionQueue();
			loaded = true;
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not load the review queue');
		}
	}

	$effect(() => {
		load();
		// New transcripts, edits and reviews change the queue; so does a scan finishing.
		return wsState.onMessage((raw) => {
			const msg = raw as { type?: string; job?: Job };
			if (msg.type === 'session' || msg.type === 'history') load();
			if (msg.type === 'job' && msg.job?.kind === 'supervision_scan' && msg.job.status === 'completed') {
				scanning = false;
				load();
			}
		});
	});

	async function scan() {
		scanning = true;
		try {
			await scanForCompliancePhrases();
		} catch (e) {
			scanning = false;
			toastState.error(e instanceof Error ? e.message : 'Could not start the check');
		}
	}

	async function open(item: SupervisionQueueItem) {
		sessionState.pendingOpen = { sessionId: item.session_id, idx: null };
		onOpenSession?.();
		await sessionState.selectSession(item.session_id);
	}

	const shown = $derived(items.filter((i) => show === 'all' || (show === 'reviewed') === i.reviewed));
	const waiting = $derived(items.filter((i) => !i.reviewed).length);
	const date = (iso: string) => new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
</script>

<div class="space-y-4">
	<div>
		<h2 class="text-xl font-semibold text-gray-100">Review</h2>
		<p class="text-sm text-gray-500 mt-1">
			Meetings where someone used a compliance phrase (Settings → General → Supervision). Read each flagged line in context,
			then mark the meeting reviewed with a note; the review goes into its history.
		</p>
	</div>

	<div class="flex flex-wrap items-center gap-2 text-sm">
		<div class="flex rounded-lg border border-gray-800 bg-gray-900 p-0.5" role="group" aria-label="Show">
			{#each [['open', `To review (${waiting})`], ['reviewed', 'Reviewed'], ['all', 'All']] as [value, label] (value)}
				<button
					onclick={() => (show = value as typeof show)}
					aria-pressed={show === value}
					class="px-3 py-1 rounded-md text-xs {show === value ? 'bg-gray-700 text-gray-100' : 'text-gray-400 hover:text-gray-200'}"
				>{label}</button>
			{/each}
		</div>
		{#if isAdmin}
			<button onclick={scan} disabled={scanning} class="ml-auto text-xs text-blue-400 hover:text-blue-300 disabled:opacity-50" title="For meetings from before supervision was on">
				{scanning ? 'Checking every meeting…' : 'Check every meeting again'}
			</button>
		{/if}
	</div>

	{#if !loaded}
		<p class="text-sm text-gray-500">Loading…</p>
	{:else if shown.length === 0}
		<p class="text-sm text-gray-500">
			{show === 'open' ? 'Nothing waiting for review.' : show === 'reviewed' ? 'No meeting has been reviewed yet.' : 'No meeting has a flagged line.'}
		</p>
	{:else}
		<ul class="divide-y divide-gray-800 rounded-lg border border-gray-800">
			{#each shown as item (item.session_id)}
				<li>
					<button onclick={() => open(item)} class="w-full text-left px-3 py-2 hover:bg-gray-900">
						<div class="flex flex-wrap items-baseline gap-x-2">
							<span class="text-gray-100">{item.name}</span>
							<span class="text-xs text-gray-500">{date(item.created_at)}{item.owner ? ` · ${item.owner}` : ''}</span>
							<span class="ml-auto text-xs {item.reviewed ? 'text-green-400' : 'text-amber-300'}">
								{item.reviewed ? 'Reviewed' : `${item.flags} line${item.flags === 1 ? '' : 's'} to review`}
							</span>
						</div>
						<div class="mt-1 flex flex-wrap gap-1">
							{#each item.phrases as phrase (phrase)}
								<span class="rounded bg-gray-800 px-1.5 text-xs text-gray-300">{phrase}</span>
							{/each}
						</div>
						{#if item.reviewed_at}
							<p class="mt-1 text-xs text-gray-500">
								{item.reviewed ? '' : 'Last reviewed '}{date(item.reviewed_at)}{item.reviewed_by ? ` by ${item.reviewed_by}` : ''}{item.note ? `: ${item.note}` : ''}
							</p>
						{/if}
					</button>
				</li>
			{/each}
		</ul>
	{/if}
</div>
