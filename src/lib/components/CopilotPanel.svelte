<script lang="ts">
	import { askCopilot, getCopilotNotes } from '$lib/api/backend.js';
	import { audioState } from '$lib/stores/audio.svelte.js';
	import { jobsState } from '$lib/stores/jobs.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { wsState } from '$lib/stores/websocket.svelte.js';
	import type { BackendEvent, CopilotNotes } from '$lib/types/index.js';

	let notes = $state<CopilotNotes | null>(null);
	let question = $state('');
	let asks = $state<{ jobId: string; question: string }[]>([]);

	const sessionId = $derived(sessionState.activeSession?.id ?? null);
	const recordingHere = $derived(audioState.isRecording && audioState.activeSessionId === sessionId);

	$effect(() => {
		const id = sessionId;
		notes = null;
		asks = [];
		if (!id) return;
		getCopilotNotes(id)
			.then((n) => {
				if (sessionId === id) notes = n;
			})
			.catch(() => {});
		return wsState.onMessage((raw) => {
			const msg = raw as BackendEvent;
			if (msg.type === 'copilot_notes' && msg.session_id === id) notes = msg.notes;
		});
	});

	async function ask() {
		const q = question.trim();
		if (!q || !sessionId) return;
		try {
			const job = await askCopilot(sessionId, q);
			jobsState.track(job);
			asks = [{ jobId: job.id, question: q }, ...asks];
			question = '';
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not ask');
		}
	}

	function answerOf(jobId: string): { state: 'pending' | 'done' | 'failed'; text: string } {
		const j = jobsState.jobs[jobId];
		if (!j || j.status === 'queued' || j.status === 'running') return { state: 'pending', text: '' };
		if (j.status === 'completed') return { state: 'done', text: (j.result as { answer?: string } | null)?.answer ?? '' };
		return { state: 'failed', text: j.error ?? 'Failed' };
	}

	const clock = (iso: string) => new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
	const empty = $derived(
		!notes || (!notes.summary.length && !notes.decisions.length && !notes.action_items.length && !notes.open_questions.length)
	);
</script>

{#if recordingHere || notes}
	<section class="rounded-lg border border-purple-900/60 bg-purple-950/20 p-3 space-y-3" aria-label="Copilot">
		<header class="flex items-center gap-2 text-xs">
			<span class="font-semibold text-purple-200 uppercase tracking-wide">Copilot</span>
			<span class="text-purple-300/60">
				{#if notes}updated {clock(notes.updated_at)} · {notes.lines} lines{:else}running notes appear after the first minutes of talk{/if}
			</span>
		</header>

		{#if !empty && notes}
			<div class="grid gap-3 sm:grid-cols-2 text-sm">
				{#if notes.summary.length}
					<div class="sm:col-span-2">
						<h4 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 mb-1">So far</h4>
						<ul class="list-disc list-inside space-y-0.5 text-gray-200">{#each notes.summary as x}<li>{x}</li>{/each}</ul>
					</div>
				{/if}
				{#if notes.decisions.length}
					<div>
						<h4 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 mb-1">Decided</h4>
						<ul class="space-y-0.5 text-gray-200">{#each notes.decisions as x}<li>✔ {x}</li>{/each}</ul>
					</div>
				{/if}
				{#if notes.action_items.length}
					<div>
						<h4 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 mb-1">To do</h4>
						<ul class="space-y-0.5 text-gray-200">
							{#each notes.action_items as a}<li>○ {a.text}{#if a.owner}<span class="text-gray-500"> · {a.owner}</span>{/if}</li>{/each}
						</ul>
					</div>
				{/if}
				{#if notes.open_questions.length}
					<div class="sm:col-span-2">
						<h4 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 mb-1">Open</h4>
						<ul class="space-y-0.5 text-gray-300">{#each notes.open_questions as x}<li>? {x}</li>{/each}</ul>
					</div>
				{/if}
			</div>
		{/if}

		<form onsubmit={(e) => { e.preventDefault(); ask(); }} class="flex gap-2">
			<input
				bind:value={question}
				placeholder="Ask about this meeting… (e.g. what did they just say about the budget?)"
				aria-label="Ask about this meeting"
				class="flex-1 bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100 placeholder-gray-600"
			/>
			<button type="submit" disabled={!question.trim()} class="px-3 py-1.5 text-sm rounded bg-purple-600 hover:bg-purple-700 disabled:bg-gray-700 text-white">Ask</button>
		</form>
		{#each asks as a (a.jobId)}
			{@const ans = answerOf(a.jobId)}
			<div class="text-sm">
				<p class="text-gray-400">{a.question}</p>
				<p class={ans.state === 'failed' ? 'text-red-400' : 'text-gray-100'} aria-live="polite">
					{ans.state === 'pending' ? '…' : ans.text}
				</p>
			</div>
		{/each}
	</section>
{/if}
