<script lang="ts">
	import { listNarrations, listVoices, narrate, narrationAudioUrl, getNarration } from '$lib/api/backend.js';
	import { plainError, waitForJob } from '$lib/app/voiceRecording.js';
	import { jobsState } from '$lib/stores/jobs.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { uiState } from '$lib/stores/ui.svelte.js';
	import type { Narration, Voice } from '$lib/types/index.js';

	// "Listen" on a summary, notes or a digest: read in one of your ready narration voices
	// (services/narration.py), then a player that highlights the part being read.
	let {
		sourceKind,
		sourceId,
		label = 'Listen'
	}: { sourceKind: 'summary' | 'notes' | 'digest' | 'followup'; sourceId: string; label?: string } = $props();

	let open = $state(false);
	let voices = $state<Voice[]>([]);
	let voiceId = $state('');
	let narration = $state<Narration | null>(null);
	let progress = $state('');
	let time = $state(0);
	const ready = $derived(voices.filter((v) => v.ready));
	const current = $derived(narration?.chunks.findIndex((c) => time >= c.start && time < c.end + 0.3) ?? -1);

	async function toggle() {
		open = !open;
		if (!open) return;
		voices = await listVoices().catch(() => []);
		if (!voiceId && ready.length) voiceId = ready[0].id;
		const earlier = await listNarrations(sourceKind, sourceId).catch(() => []);
		narration = earlier[0] ?? null;
	}

	async function start() {
		if (!voiceId) return;
		progress = 'Starting…';
		try {
			const job = await narrate({ voice_id: voiceId, source_kind: sourceKind, source_id: sourceId, text: '', title: '', check: true });
			const watch = setInterval(() => {
				progress = jobsState.jobs[job.id]?.message || progress;
			}, 500);
			const done = await waitForJob(job);
			clearInterval(watch);
			if (done.status !== 'completed') throw new Error(done.error || 'The narration failed');
			narration = await getNarration((done.result as { narration_id: string }).narration_id);
		} catch (e) {
			toastState.error(plainError(e, 'Could not narrate'));
		} finally {
			progress = '';
		}
	}

	function openVoices() {
		uiState.settingsTab = 'voices';
		uiState.view = 'settings';
	}
</script>

<span class="relative inline-block">
<button
	onclick={toggle}
	class="text-gray-500 hover:text-gray-300 text-xs px-2 py-1 rounded bg-gray-800 border border-gray-700 transition-colors"
	title="Read it aloud in your own narration voice"
>{label}</button>

{#if open}
	<div class="absolute right-0 z-20 mt-1 w-96 max-w-[90vw] rounded border border-gray-700 bg-gray-900 p-3 space-y-2 text-xs shadow-lg text-left">
		{#if !ready.length}
			<p class="text-gray-400">
				You have no narration voice ready yet. <button onclick={openVoices} class="underline hover:text-gray-200">Make one in Settings → Voices</button>.
			</p>
		{:else}
			<div class="flex items-center gap-2">
				<select bind:value={voiceId} class="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-gray-200">
					{#each ready as v (v.id)}<option value={v.id}>{v.name}</option>{/each}
				</select>
				<button onclick={start} disabled={!!progress} class="px-3 py-1 rounded bg-blue-700 hover:bg-blue-600 text-white disabled:opacity-50">
					{narration ? 'Narrate again' : 'Narrate'}
				</button>
				{#if progress}<span class="text-gray-400">{progress}</span>{/if}
			</div>
		{/if}
		{#if narration}
			<audio controls src={narrationAudioUrl(narration.id)} ontimeupdate={(e) => (time = (e.currentTarget as HTMLAudioElement).currentTime)} class="w-full"></audio>
			<ol class="space-y-0.5 max-h-48 overflow-y-auto">
				{#each narration.chunks as c, i}
					<li class="{i === current ? 'text-gray-100 bg-gray-800 rounded px-1' : 'text-gray-500'}">
						{c.text}{#if c.flagged}<span class="ml-1 text-amber-400" title="This part may not read exactly as written">⚠</span>{/if}
					</li>
				{/each}
			</ol>
			<p class="text-[10px] text-gray-600">Synthetic speech in {narration.voice_name}'s voice, marked as such · {narration.model}</p>
		{/if}
	</div>
{/if}
</span>
