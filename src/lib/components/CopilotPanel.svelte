<script lang="ts">
	import { askCopilot, getCopilotNotes } from '$lib/api/backend.js';
	import { audioState } from '$lib/stores/audio.svelte.js';
	import { jobsState } from '$lib/stores/jobs.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { wsState } from '$lib/stores/websocket.svelte.js';
	import { canChange } from '$lib/app/access.js';
	import { appendToDoc, appendToText, type CopilotItem } from '$lib/notes/copilotLines.js';
	import type { NotesDocState } from '$lib/notes/doc.svelte.js';
	import { acquireNotes, releaseNotes } from '$lib/stores/notes.svelte.js';
	import AgendaCard from './AgendaCard.svelte';
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

	let asking = $state(false);
	async function ask() {
		const q = question.trim();
		if (!q || !sessionId || asking) return;
		asking = true;
		try {
			const job = await askCopilot(sessionId, q);
			jobsState.track(job);
			asks = [{ jobId: job.id, question: q }, ...asks];
			question = '';
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not ask');
		} finally {
			asking = false;
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

	// ---- "Add to notes": the meeting's notes document is shared with the notes editor ---------------
	let notesDoc = $state.raw<NotesDocState | null>(null);
	let added = $state<string | null>(null);
	let addedTimer: ReturnType<typeof setTimeout> | null = null;

	const mayAdd = $derived(!empty);
	// Only open the notes connection when there is something to add.
	$effect(() => {
		const id = sessionId;
		if (!id || !mayAdd) {
			notesDoc = null;
			return;
		}
		const st = acquireNotes(id);
		notesDoc = st;
		return () => {
			st.flush();
			releaseNotes(id);
		};
	});
	$effect(() => () => {
		if (addedTimer) clearTimeout(addedTimer);
	});
	// Live notes: the server says who may edit (shared-with included); the plain-text fallback
	// keeps the old rule.
	const canAdd = $derived(
		mayAdd &&
			!!notesDoc &&
			(notesDoc.status === 'unavailable'
				? canChange(sessionState.activeSession)
				: !notesDoc.readOnly && notesDoc.status !== 'gone')
	);

	async function addToNotes(item: CopilotItem, key: string) {
		const id = sessionId;
		const st = notesDoc;
		const session = sessionState.activeSession;
		if (!id || !st || !session || session.id !== id) return;
		let ok = false;
		if (st.status === 'unavailable') {
			// No live notes on this backend: add to the plain-text notes the old editor shows.
			ok = await sessionState.updateNotes(id, appendToText(session.notes, item));
		} else if (st.doc) {
			ok = st.applyLocal((doc) => appendToDoc(doc, item)) !== undefined;
		} else {
			toastState.info('The notes are still connecting; try again in a moment');
		}
		if (!ok) return;
		added = key;
		if (addedTimer) clearTimeout(addedTimer);
		addedTimer = setTimeout(() => {
			added = null;
			addedTimer = null;
		}, 1500);
	}
</script>

{#snippet add(item: CopilotItem, key: string)}
		{#if canAdd}
			<button
				type="button"
				onclick={() => addToNotes(item, key)}
				aria-label="Add to notes: {item.text}"
				class="ml-2 text-xs text-purple-300 hover:text-purple-100 focus:opacity-100 group-hover:opacity-100 group-focus-within:opacity-100 {added === key ? 'opacity-100' : 'opacity-0'}"
				>{added === key ? '✓ Added' : '+ Add to notes'}</button
			>
		{/if}
	{/snippet}

{#if recordingHere || notes}
	<section class="rounded-lg border border-purple-900/60 bg-purple-950/20 p-3 space-y-3" aria-label="Copilot">
		<header class="flex items-center gap-2 text-xs">
			<span class="font-semibold text-purple-200 uppercase tracking-wide">Copilot</span>
			<span class="text-purple-300/60">
				{#if notes}updated {clock(notes.updated_at)} · {notes.lines} lines{:else}running notes appear after the first minutes of talk{/if}
			</span>
		</header>

		{#if recordingHere}
			<AgendaCard covered={notes?.agenda_covered ?? []} compact />
		{/if}
		{#if !empty && notes}
			<div class="grid gap-3 sm:grid-cols-2 text-sm">
				{#if notes.summary.length}
					<div class="sm:col-span-2">
						<h4 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 mb-1">So far</h4>
						<ul class="list-disc list-inside space-y-0.5 text-gray-200">{#each notes.summary as x, i}<li class="group">{x}{@render add({ kind: 'summary', text: x }, `summary:${i}`)}</li>{/each}</ul>
					</div>
				{/if}
				{#if notes.decisions.length}
					<div>
						<h4 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 mb-1">Decided</h4>
						<ul class="space-y-0.5 text-gray-200">{#each notes.decisions as x, i}<li class="group">✔ {x}{@render add({ kind: 'decision', text: x }, `decision:${i}`)}</li>{/each}</ul>
					</div>
				{/if}
				{#if notes.action_items.length}
					<div>
						<h4 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 mb-1">To do</h4>
						<ul class="space-y-0.5 text-gray-200">
							{#each notes.action_items as a, i}<li class="group">○ {a.text}{#if a.owner}<span class="text-gray-500"> · {a.owner}</span>{/if}{@render add({ kind: 'action', text: a.text, owner: a.owner }, `action:${i}`)}</li>{/each}
						</ul>
					</div>
				{/if}
				{#if notes.open_questions.length}
					<div class="sm:col-span-2">
						<h4 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 mb-1">Open</h4>
						<ul class="space-y-0.5 text-gray-300">{#each notes.open_questions as x, i}<li class="group">? {x}{@render add({ kind: 'question', text: x }, `question:${i}`)}</li>{/each}</ul>
					</div>
				{/if}
			</div>
		{/if}

		<form onsubmit={(e) => { e.preventDefault(); ask(); }} class="flex gap-2">
			<input
				bind:value={question}
				placeholder="Ask about this meeting… (e.g. what did they just say about the budget?)"
				aria-label="Ask about this meeting"
				class="flex-1 min-w-0 bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100 placeholder-gray-600"
			/>
			<button type="submit" disabled={!question.trim() || asking} class="shrink-0 px-3 py-1.5 text-sm rounded bg-purple-600 hover:bg-purple-700 disabled:bg-gray-700 text-white">Ask</button>
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
