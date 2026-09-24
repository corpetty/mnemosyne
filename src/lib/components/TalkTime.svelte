<script lang="ts">
	import { getSessionStats } from '$lib/api/backend.js';
	import { playerState } from '$lib/stores/player.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { transcriptState } from '$lib/stores/transcript.svelte.js';
	import type { MeetingStats } from '$lib/types/index.js';

	let stats = $state<MeetingStats | null>(null);
	let open = $state(false);

	// Refetch when the session or its transcript (speakers, lines) changes.
	const key = $derived.by(() => {
		const s = sessionState.activeSession;
		if (!s || s.transcript.length === 0) return null;
		return `${s.id}:${s.transcript.length}:${s.participants.join('|')}:${s.updated_at}`;
	});
	$effect(() => {
		const k = key;
		const id = sessionState.activeSession?.id;
		if (!k || !id) {
			stats = null;
			return;
		}
		getSessionStats(id)
			.then((st) => {
				if (sessionState.activeSession?.id === id) stats = st;
			})
			.catch(() => (stats = null));
	});

	function fmt(sec: number): string {
		const m = Math.floor(sec / 60);
		const s = Math.round(sec % 60);
		return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}:${String(s).padStart(2, '0')}`;
	}

	function jump(start: number, idx: number) {
		if (playerState.available) playerState.seek(start);
		transcriptState.highlightIndex = idx;
	}
</script>

{#if stats && stats.duration_seconds > 0}
	{@const dur = stats.duration_seconds}
	<div class="space-y-2">
		<div class="relative h-3 rounded bg-gray-900 border border-gray-800 overflow-hidden" aria-label="Who spoke when">
			{#each stats.timeline as t (t.first_idx)}
				<button
					onclick={() => jump(t.start, t.first_idx)}
					title="{t.speaker} · {fmt(t.start)}–{fmt(t.end)}"
					aria-label="{t.speaker} at {fmt(t.start)}"
					class="absolute top-0 bottom-0 opacity-80 hover:opacity-100 {transcriptState.getSpeakerBg(t.speaker)}"
					style="left: {(100 * t.start) / dur}%; width: max(2px, {(100 * (t.end - t.start)) / dur}%)"
				></button>
			{/each}
		</div>
		<button onclick={() => (open = !open)} class="text-[11px] text-gray-500 hover:text-gray-300">
			{open ? '▾' : '▸'} Talk time · {fmt(dur)} · {stats.turns} turns{#if stats.silence_seconds > 30} · {fmt(stats.silence_seconds)} silence{/if}
		</button>
		{#if open}
			<table class="text-xs text-gray-400 w-full max-w-xl">
				<tbody>
					{#each stats.speakers as s (s.speaker)}
						<tr>
							<td class="py-0.5 pr-3 font-medium whitespace-nowrap {transcriptState.getSpeakerColor(s.speaker)}">{s.speaker}</td>
							<td class="py-0.5 pr-3 w-full">
								<div class="h-1.5 rounded bg-gray-800">
									<div class="h-1.5 rounded {transcriptState.getSpeakerBg(s.speaker)}" style="width: {100 * s.share}%"></div>
								</div>
							</td>
							<td class="py-0.5 pr-3 text-right tabular-nums">{Math.round(100 * s.share)}%</td>
							<td class="py-0.5 pr-3 text-right tabular-nums">{fmt(s.talk_seconds)}</td>
							<td class="py-0.5 pr-3 text-right tabular-nums whitespace-nowrap" title="Turns">{s.turns} turns</td>
							<td class="py-0.5 pr-3 text-right tabular-nums whitespace-nowrap" title="Longest uninterrupted turn">max {fmt(s.longest_turn_seconds)}</td>
							<td class="py-0.5 text-right tabular-nums whitespace-nowrap" title="Words per minute">{Math.round(s.words_per_minute)} wpm</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	</div>
{/if}
