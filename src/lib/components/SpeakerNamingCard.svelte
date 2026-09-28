<script lang="ts">
	import { listSpeakers, renameSessionSpeaker, setSpeakersReviewed } from '$lib/api/backend.js';
	import { playerState } from '$lib/stores/player.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { transcriptState } from '$lib/stores/transcript.svelte.js';

	// After transcription: one row per speaker still called SPEAKER_nn / Speaker n, with how
	// long they talked, a short sample to listen to (never plays by itself) and a name field.
	const UNNAMED = /^(SPEAKER_\d+|Speaker \d+)$/;
	const SAMPLE = 8; // seconds

	let knownVoices = $state<string[]>([]);
	let names = $state<Record<string, string>>({});
	let saveVoice = $state(true);
	let saving = $state(false);

	$effect(() => {
		listSpeakers()
			.then((v) => (knownVoices = v.map((x) => x.name)))
			.catch(() => {});
	});

	const session = $derived(sessionState.activeSession);

	interface Row {
		label: string;
		seconds: number;
		sample: { start: number; end: number } | null;
	}

	const rows = $derived.by((): Row[] => {
		const byLabel = new Map<string, Row>();
		const longest = new Map<string, number>();
		for (const seg of session?.transcript ?? []) {
			if (!UNNAMED.test(seg.speaker)) continue;
			const row = byLabel.get(seg.speaker) ?? { label: seg.speaker, seconds: 0, sample: null };
			const length = seg.end - seg.start;
			row.seconds += length;
			if (length > (longest.get(seg.speaker) ?? 0)) {
				longest.set(seg.speaker, length);
				row.sample = { start: seg.start, end: Math.min(seg.end, seg.start + SAMPLE) };
			}
			byLabel.set(seg.speaker, row);
		}
		return [...byLabel.values()].sort((a, b) => b.seconds - a.seconds);
	});

	// Invitees not yet in this transcript first, then saved voices.
	const suggestions = $derived.by(() => {
		const present = new Set(session?.transcript.map((s) => s.speaker) ?? []);
		return [...new Set([...(session?.attendees ?? []), ...knownVoices])].filter((n) => !present.has(n));
	});

	const show = $derived(!!session && !session.speakers_reviewed && rows.length > 0);

	function fmt(sec: number): string {
		const m = Math.floor(sec / 60);
		return m ? `${m} min` : `${Math.round(sec)} s`;
	}

	async function done() {
		if (!session) return;
		saving = true;
		const id = session.id;
		let named = 0;
		try {
			for (const row of rows) {
				const name = (names[row.label] ?? '').trim();
				if (!name || name === row.label) continue;
				sessionState.activeSession = await renameSessionSpeaker(id, row.label, name, saveVoice);
				named++;
			}
			const updated = await setSpeakersReviewed(id);
			sessionState.activeSession = updated;
			transcriptState.showSession(updated.id, updated.transcript);
			if (named) toastState.success(saveVoice ? `Named ${named}; their voices will be recognized next time` : `Named ${named}`);
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not rename the speakers');
		} finally {
			saving = false;
		}
	}

	async function skip() {
		if (!session) return;
		try {
			sessionState.activeSession = await setSpeakersReviewed(session.id);
		} catch {
			/* it simply shows again next time */
		}
	}
</script>

{#if show}
	<section class="rounded-lg border border-blue-900/60 bg-blue-950/20 p-3 space-y-2" aria-label="Who is who">
		<div class="flex items-baseline gap-2">
			<h4 class="text-sm font-medium text-gray-200">Who is who?</h4>
			<span class="text-xs text-gray-500">Name the speakers; listen to a sample if unsure.</span>
		</div>
		<datalist id="speaker-card-suggestions">
			{#each suggestions as n}<option value={n}></option>{/each}
		</datalist>
		<ul class="space-y-1.5">
			{#each rows as row (row.label)}
				<li class="flex items-center gap-2 text-sm">
					<button
						onclick={() => row.sample && playerState.playRange(row.sample.start, row.sample.end)}
						disabled={!row.sample || !playerState.available}
						class="w-7 h-7 shrink-0 rounded-full bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 text-xs disabled:opacity-40"
						title="Play a sample of {row.label}"
						aria-label="Play a sample of {row.label}"
					>▶</button>
					<span class="w-28 shrink-0 text-gray-400 truncate">{row.label}</span>
					<span class="w-14 shrink-0 text-xs text-gray-600">{fmt(row.seconds)}</span>
					<input
						bind:value={names[row.label]}
						list="speaker-card-suggestions"
						placeholder="Their name"
						aria-label="Name for {row.label}"
						class="flex-1 min-w-0 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-200"
					/>
				</li>
			{/each}
		</ul>
		<div class="flex items-center gap-3">
			<label class="flex items-center gap-1.5 text-xs text-gray-400">
				<input type="checkbox" bind:checked={saveVoice} class="rounded border-gray-600 bg-gray-800" />
				Remember their voices
			</label>
			<button onclick={skip} class="ml-auto text-xs text-gray-500 hover:text-gray-300">Not now</button>
			<button
				onclick={done}
				disabled={saving}
				class="px-3 py-1 text-xs rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50"
			>{saving ? 'Saving…' : 'Done'}</button>
		</div>
	</section>
{/if}
