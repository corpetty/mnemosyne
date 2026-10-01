<script lang="ts">
	import { getMeetingRecord, getRecordVersion, setLegalHold } from '$lib/api/backend.js';
	import { exportRecords } from '$lib/app/records.js';
	import { connectionState } from '$lib/stores/connection.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { MeetingRecord, RecordVersion } from '$lib/types/index.js';

	// The meeting as a record (backend services/records.py): how long it is kept, a legal hold,
	// its seals checked against what is there now, earlier versions, and an export of it.
	let record = $state<MeetingRecord | null>(null);
	let open = $state(false);
	let shown = $state<RecordVersion | null>(null);
	let exporting = $state(false);
	const sessionId = $derived(sessionState.activeSession?.id ?? null);
	const canHold = $derived(!connectionState.me?.id || ['reviewer', 'admin'].includes(connectionState.me.role));
	const hasContent = $derived(
		!!sessionState.activeSession && (sessionState.activeSession.transcript.length > 0 || !!sessionState.activeSession.summary)
	);

	async function load() {
		if (!sessionId) return;
		try {
			record = await getMeetingRecord(sessionId);
		} catch {
			record = null;
		}
	}

	$effect(() => {
		// Checked when opened (it hashes the audio the first time), and again when the meeting changes.
		if (open && sessionId && sessionState.activeSession?.updated_at) void load();
	});

	async function hold() {
		if (!sessionId) return;
		const reason = prompt('Legal hold: why? (a matter or request number). Nothing of this meeting can be deleted until it is lifted.');
		if (!reason?.trim()) return;
		record = await setLegalHold(sessionId, reason.trim());
	}

	async function lift() {
		if (!sessionId || !confirm('Lift the legal hold on this meeting?')) return;
		record = await setLegalHold(sessionId, '');
	}

	async function exportThis() {
		if (!sessionId) return;
		exporting = true;
		try {
			await exportRecords({ session_ids: [sessionId] });
			toastState.info('Export ready: downloading');
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not export');
		} finally {
			exporting = false;
		}
	}

	async function show(id: number) {
		if (!sessionId) return;
		shown = shown?.id === id ? null : await getRecordVersion(sessionId, id);
	}

	const when = (iso: string) => new Date(iso).toLocaleString();
</script>

{#if hasContent}
	<details bind:open class="rounded-lg border border-gray-800 bg-gray-900/40 px-3 py-2 text-sm">
		<summary class="cursor-pointer text-gray-300">
			Record
			{#if record?.legal_hold}<span class="ml-2 rounded bg-amber-900/60 px-1.5 text-xs text-amber-200">legal hold</span>{/if}
			{#if record && !record.verification.ok}<span class="ml-2 rounded bg-red-900/60 px-1.5 text-xs text-red-200">changed outside the app</span>{/if}
		</summary>
		{#if record}
			<div class="mt-2 space-y-2 text-xs text-gray-400">
				<p>
					{#if record.kept_until}Kept as a record until {record.kept_until}: deleting it or its audio needs an admin and a reason.{:else}No records period is set.{/if}
				</p>
				<p>
					{#if record.legal_hold}
						<span class="text-amber-200">On legal hold: {record.legal_hold}.</span> Nothing of it can be deleted.
						{#if canHold}<button onclick={lift} class="ml-1 text-blue-400 hover:text-blue-300">Lift</button>{/if}
					{:else if canHold}
						<button onclick={hold} class="text-blue-400 hover:text-blue-300">Put on legal hold</button>
					{/if}
				</p>
				{#if record.verification.seals}
					{#if record.verification.ok}
						<p class="text-green-300">
							Sealed {record.verification.seals} time{record.verification.seals === 1 ? '' : 's'}, last {record.verification.last_sealed_at ? when(record.verification.last_sealed_at) : ''}: unchanged since.
						</p>
					{:else}
						<ul class="list-disc pl-4 text-red-300">
							{#each record.verification.problems as p}<li>{p}</li>{/each}
						</ul>
					{/if}
					<p class="break-all text-gray-600" title="Proves this record later: an export carries it too">Chain head {record.verification.chain_head}</p>
				{/if}
				{#if record.versions.length}
					<div>
						<p class="text-gray-300">Earlier versions</p>
						<ul class="space-y-0.5">
							{#each [...record.versions].reverse() as v (v.id)}
								<li>
									<button onclick={() => show(v.id)} class="text-left hover:text-gray-200">
										{when(v.at)} · replaced when {v.reason}{v.by ? ` by ${v.by}` : ''}
									</button>
									{#if shown?.id === v.id}
										<div class="mt-1 max-h-60 overflow-y-auto rounded border border-gray-800 bg-gray-950 p-2 text-gray-300 whitespace-pre-wrap">
											{#if shown.summary}<p class="mb-2">{shown.summary}</p>{/if}
											{#each shown.transcript as line}<p><span class="text-gray-500">{line.speaker}:</span> {line.text}</p>{/each}
										</div>
									{/if}
								</li>
							{/each}
						</ul>
					</div>
				{/if}
				<button onclick={exportThis} disabled={exporting} class="rounded border border-gray-700 bg-gray-800 px-2 py-1 text-gray-200 hover:bg-gray-700 disabled:opacity-50">
					{exporting ? 'Preparing the export…' : 'Export the record'}
				</button>
			</div>
		{:else}
			<p class="mt-2 text-xs text-gray-500">Checking…</p>
		{/if}
	</details>
{/if}
