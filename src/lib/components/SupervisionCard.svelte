<script lang="ts">
	import { getMeetingSupervision, markReviewed } from '$lib/api/backend.js';
	import { canReview } from '$lib/app/supervision.js';
	import { connectionState } from '$lib/stores/connection.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { transcriptState } from '$lib/stores/transcript.svelte.js';
	import type { MeetingSupervision } from '$lib/types/index.js';

	// A meeting's lines with compliance phrases (backend services/supervision.py) and its
	// reviews, above the transcript, for reviewers and admins.
	let data = $state<MeetingSupervision | null>(null);
	let note = $state('');
	let saving = $state(false);
	let open = $state(true);
	const sessionId = $derived(sessionState.activeSession?.id ?? null);
	const me = $derived(connectionState.me);
	const allowed = $derived(canReview(me));

	$effect(() => {
		// Again when the meeting changes: an edit or a new transcription changes its flags.
		const id = sessionId;
		void sessionState.activeSession?.updated_at;
		if (!allowed || !id) {
			data = null;
			return;
		}
		getMeetingSupervision(id)
			.then((d) => {
				if (id !== sessionId) return;
				if (data === null || data.reviewed !== d.reviewed) open = !d.reviewed;
				data = d;
			})
			.catch(() => (data = null));
	});

	async function review() {
		if (!sessionId) return;
		saving = true;
		try {
			data = await markReviewed(sessionId, note.trim());
			note = '';
			open = false;
			toastState.success('Marked reviewed');
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not mark it reviewed');
		} finally {
			saving = false;
		}
	}

	const time = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
	const when = (iso: string) => new Date(iso).toLocaleString();

	/** The line with its phrase marked. */
	function parts(text: string, phrase: string): [string, string, string] {
		const words = phrase.split(/\s+/).map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/'/g, "['\u2019]"));
		const m = new RegExp(`(?<!\\w)${words.join('\\s+')}(?!\\w)`, 'i').exec(text);
		return m ? [text.slice(0, m.index), m[0], text.slice(m.index + m[0].length)] : [text, '', ''];
	}
</script>

{#if data && data.flags.length}
	<details bind:open class="mb-4 rounded-lg border {data.reviewed ? 'border-gray-800' : 'border-amber-800/60'} bg-gray-900/40 px-3 py-2 text-sm">
		<summary class="cursor-pointer text-gray-300">
			Supervision: {data.flags.length} flagged line{data.flags.length === 1 ? '' : 's'}
			{#if data.reviewed}<span class="ml-2 rounded bg-green-900/50 px-1.5 text-xs text-green-300">reviewed</span>
			{:else}<span class="ml-2 rounded bg-amber-900/60 px-1.5 text-xs text-amber-200">to review</span>{/if}
		</summary>
		<ul class="mt-2 space-y-1">
			{#each data.flags as f (f.start + f.phrase + f.text)}
				{@const [before, hit, after] = parts(f.text, f.phrase)}
				<li>
					<button onclick={() => (transcriptState.highlightIndex = f.idx)} class="w-full text-left rounded px-1 py-0.5 hover:bg-gray-800" title="Show it in the transcript">
						<span class="text-xs text-gray-500 tabular-nums">{time(f.start)}</span>
						<span class="text-xs text-gray-400">{f.speaker}:</span>
						<span class="text-gray-300">{before}<mark class="rounded bg-amber-900/60 px-0.5 text-amber-100">{hit}</mark>{after}</span>
					</button>
				</li>
			{/each}
		</ul>
		{#if data.reviews.length}
			<ul class="mt-2 space-y-0.5 text-xs text-gray-500">
				{#each data.reviews as r (r.at)}
					<li>Reviewed {when(r.at)}{r.by ? ` by ${r.by}` : ''}{r.note ? `: ${r.note}` : ''}</li>
				{/each}
			</ul>
		{/if}
		<div class="mt-2 flex flex-wrap items-end gap-2">
			<label class="min-w-0 flex-1 text-xs text-gray-400">
				Note (what you checked, what follows)
				<input bind:value={note} maxlength="2000" class="mt-1 block w-full rounded border border-gray-700 bg-gray-800 px-2 py-1 text-sm text-gray-100" />
			</label>
			<button onclick={review} disabled={saving} class="rounded border border-gray-700 bg-gray-800 px-3 py-1.5 text-gray-200 hover:bg-gray-700 disabled:opacity-50">
				{data.reviewed ? 'Review again' : 'Mark reviewed'}
			</button>
		</div>
	</details>
{/if}
