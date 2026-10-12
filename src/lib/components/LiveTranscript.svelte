<script lang="ts">
	import { transcriptState } from '$lib/stores/transcript.svelte.js';
	import { audioState } from '$lib/stores/audio.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { wsState } from '$lib/stores/websocket.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { LiveLine } from '$lib/stores/transcript.svelte.js';

	// Tall while recording, next to the copilot; compact elsewhere.
	let { tall = false }: { tall?: boolean } = $props();

	function formatTime(seconds: number): string {
		const m = Math.floor(seconds / 60);
		const s = Math.floor(seconds % 60);
		return `${m}:${String(s).padStart(2, '0')}`;
	}

	let container = $state<HTMLDivElement>();
	// Follow new lines only while the view is at the bottom: someone scrolled up to reread
	// something is not yanked back down every few seconds.
	let following = true;
	function onScroll() {
		if (container) following = container.scrollHeight - container.scrollTop - container.clientHeight < 40;
	}
	$effect(() => {
		void transcriptState.liveSegments.length;
		void transcriptState.livePartials;
		if (container && following) container.scrollTop = container.scrollHeight;
	});

	const partials = $derived(Object.values(transcriptState.livePartials).filter((p) => p.text));
	const mentioned = $derived(new Set(transcriptState.mentionStarts));
	// Only in the meeting it belongs to (another meeting may be open while recording).
	const here = $derived(
		!!transcriptState.liveSessionId && transcriptState.liveSessionId === sessionState.activeSession?.id
	);
	const recordingHere = $derived(
		audioState.isRecording && audioState.activeSessionId === transcriptState.liveSessionId
	);
	const show = $derived(
		here && (recordingHere || transcriptState.liveSegments.length > 0 || partials.length > 0)
	);

	// Lines marked "Not said": loaded again when the recording screen shows up or the
	// connection comes back, so a reopened window shows what was marked.
	$effect(() => {
		const id = transcriptState.liveSessionId;
		if (!show || !id || !wsState.connected) return;
		transcriptState.loadFlags(id).catch(() => {});
	});

	// Lines whose request is out, so a double click does not send two.
	let busy = $state<Record<string, boolean>>({});
	async function toggleFlag(line: LiveLine, flagged: boolean) {
		const key = `${line.source}|${line.rawStart ?? line.start}|${line.rawEnd ?? line.end}`;
		if (busy[key]) return;
		busy[key] = true;
		try {
			if (flagged) await transcriptState.unflagLine(line);
			else await transcriptState.flagLine(line);
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not change the mark');
		} finally {
			delete busy[key];
		}
	}
</script>

{#if show}
	<div class="rounded-lg border border-gray-800 bg-gray-900/60">
		<div class="flex items-center gap-2 px-3 py-1.5 border-b border-gray-800 text-xs text-gray-500">
			{#if recordingHere}
				<span class="w-1.5 h-1.5 rounded-full bg-red-500"></span>
				<span>Live transcript</span>
				{#if transcriptState.liveStatus}<span class="text-gray-600">· {transcriptState.liveStatus}</span>{/if}
			{:else}
				<span>Live transcript (provisional)</span>
			{/if}
		</div>
		<div bind:this={container} onscroll={onScroll} class="{tall ? 'h-[60vh]' : 'max-h-64'} overflow-y-auto p-3 space-y-1.5 text-sm">
			{#each transcriptState.liveSegments as seg}
				{@const hit = mentioned.has(seg.start)}
				{@const flagged = !!transcriptState.flagOf(seg)}
				<div class="group flex gap-3 {hit ? 'rounded bg-amber-900/30 ring-1 ring-amber-700/60' : ''}" title={hit ? 'You were mentioned' : undefined}>
					<span class="w-12 text-right text-xs font-mono text-gray-600 flex-shrink-0">{formatTime(seg.start)}</span>
					<span class="w-20 flex-shrink-0 font-medium {transcriptState.getSpeakerColor(seg.speaker)}">{seg.speaker}</span>
					<span class="flex-1 {flagged ? 'text-gray-600 line-through' : 'text-gray-300'}">{seg.text}</span>
					{#if flagged}
						<button
							type="button"
							onclick={() => toggleFlag(seg, true)}
							class="flex-shrink-0 self-start text-xs text-gray-500 hover:text-gray-200 focus:text-gray-200"
						>Undo</button>
					{:else if recordingHere && seg.source !== undefined}
						<button
							type="button"
							onclick={() => toggleFlag(seg, false)}
							aria-label="Not said"
							title="Not said"
							class="flex-shrink-0 self-start px-1 text-gray-500 hover:text-gray-200 opacity-0 group-hover:opacity-100 focus:opacity-100"
						>
							<svg viewBox="0 0 16 16" class="h-3.5 w-3.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true">
								<circle cx="8" cy="8" r="6.2" />
								<path d="M3.6 12.4 12.4 3.6" />
							</svg>
						</button>
					{/if}
				</div>
			{/each}
			{#each partials as p}
				<div class="flex gap-3 italic text-gray-500">
					<span class="w-12 flex-shrink-0"></span>
					<span class="w-20 flex-shrink-0 {transcriptState.getSpeakerColor(p.speaker)} opacity-70">{p.speaker}</span>
					<span>{p.text}…</span>
				</div>
			{/each}
			{#if transcriptState.liveSegments.length === 0 && partials.length === 0}
				<p class="text-xs text-gray-600">Listening… text appears a few seconds behind speech.</p>
			{/if}
		</div>
	</div>
{/if}
