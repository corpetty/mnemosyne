<script lang="ts">
	import { transcriptState } from '$lib/stores/transcript.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { deleteSegment, mergeSegmentUp, splitSegment, updateSegment } from '$lib/api/backend.js';
	import type { SessionDetail } from '$lib/types/index.js';
	import LiveTranscript from './LiveTranscript.svelte';
	import SpeakerBar from './SpeakerBar.svelte';

	function formatTime(seconds: number): string {
		const m = Math.floor(seconds / 60);
		const s = Math.floor(seconds % 60);
		return `${m}:${String(s).padStart(2, '0')}`;
	}

	let container = $state<HTMLDivElement>();
	let editingIdx = $state<number | null>(null);
	let draft = $state('');
	let textarea = $state<HTMLTextAreaElement>();
	let busy = $state(false);

	$effect(() => {
		// Auto-scroll to bottom when new segments arrive during processing
		if (transcriptState.isProcessing && transcriptState.segments.length > 0 && container) {
			container.scrollTop = container.scrollHeight;
		}
	});

	$effect(() => {
		// Scroll to and flash a highlighted segment (from search)
		const idx = transcriptState.highlightIndex;
		if (idx === null || !container) return;
		const el = container.querySelector<HTMLElement>(`[data-idx="${idx}"]`);
		if (el) {
			el.scrollIntoView({ block: 'center' });
			el.classList.add('bg-yellow-900/40');
			setTimeout(() => el.classList.remove('bg-yellow-900/40'), 2500);
		}
		transcriptState.highlightIndex = null;
	});

	const canTranscribe = $derived(
		!!sessionState.activeSession?.audio_file && !transcriptState.isProcessing
	);
	const canEdit = $derived(!!sessionState.activeSession && !transcriptState.isProcessing);

	async function handleTranscribe() {
		const session = sessionState.activeSession;
		if (session) await transcriptState.transcribe(session.id);
	}

	function applyUpdated(updated: SessionDetail) {
		sessionState.activeSession = updated;
		transcriptState.showSession(updated.id, updated.transcript);
	}

	async function run(op: () => Promise<SessionDetail>, okMessage?: string) {
		busy = true;
		try {
			applyUpdated(await op());
			if (okMessage) toastState.info(okMessage);
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Edit failed');
		} finally {
			busy = false;
		}
	}

	function startEdit(idx: number) {
		if (!canEdit) return;
		editingIdx = idx;
		draft = transcriptState.segments[idx].text;
		queueMicrotask(() => textarea?.focus());
	}

	async function saveEdit() {
		const session = sessionState.activeSession;
		if (!session || editingIdx === null) return;
		const idx = editingIdx;
		const text = draft.trim();
		editingIdx = null;
		if (!text || text === transcriptState.segments[idx].text) return;
		await run(() => updateSegment(session.id, idx, { text }));
	}

	async function splitAtCursor() {
		const session = sessionState.activeSession;
		if (!session || editingIdx === null || !textarea) return;
		const idx = editingIdx;
		const offset = textarea.selectionStart;
		const text = draft.trim();
		editingIdx = null;
		// Save any text change first so the split offset refers to the saved text.
		if (text !== transcriptState.segments[idx].text) {
			await run(() => updateSegment(session.id, idx, { text }));
		}
		await run(() => splitSegment(session.id, idx, offset), 'Segment split');
	}

	function onKey(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			editingIdx = null;
		} else if (e.key === 'Enter' && !e.shiftKey) {
			e.preventDefault();
			saveEdit();
		}
	}

	async function changeSpeaker(idx: number, value: string) {
		const session = sessionState.activeSession;
		if (!session) return;
		let speaker = value;
		if (value === '__new__') {
			const name = prompt('Speaker name');
			if (!name?.trim()) return;
			speaker = name.trim();
		}
		if (speaker === transcriptState.segments[idx].speaker) return;
		await run(() => updateSegment(session.id, idx, { speaker }));
	}

	async function remove(idx: number) {
		const session = sessionState.activeSession;
		if (!session) return;
		if (!confirm('Delete this segment?')) return;
		await run(() => deleteSegment(session.id, idx), 'Segment deleted');
	}

	async function mergeUp(idx: number) {
		const session = sessionState.activeSession;
		if (!session) return;
		await run(() => mergeSegmentUp(session.id, idx), 'Merged with previous');
	}
</script>

<div class="space-y-2">
	<div class="flex items-center justify-between gap-3">
		<div class="flex items-center gap-2 text-sm text-gray-400 min-h-5">
			{#if transcriptState.isProcessing}
				<span class="w-2 h-2 rounded-full bg-yellow-500 animate-pulse"></span>
			{/if}
			{#if transcriptState.status}
				<span>{transcriptState.status}</span>
			{/if}
		</div>
		{#if canTranscribe}
			<button
				onclick={handleTranscribe}
				class="px-3 py-1 text-xs rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 transition-colors"
				title="Run transcription on this session's audio"
			>
				{transcriptState.segments.length > 0 ? 'Re-transcribe' : 'Transcribe'}
			</button>
		{/if}
	</div>

	{#if transcriptState.error}
		<p class="text-sm text-red-400">{transcriptState.error}</p>
	{/if}

	{#if transcriptState.segments.length === 0}
		<LiveTranscript />
	{:else}
		<SpeakerBar />
		{#if canEdit}
			<p class="text-[11px] text-gray-600">Click text to edit (Enter saves, Esc cancels). Speaker menus reassign a line. Hover a line for merge and delete.</p>
		{/if}
	{/if}

	<div bind:this={container} class="max-h-[500px] overflow-y-auto space-y-1 pr-2">
		{#each transcriptState.segments as segment, idx (idx)}
			<div data-idx={idx} class="group flex gap-3 text-sm rounded px-1 py-1 transition-colors {editingIdx === idx ? 'bg-gray-900' : 'hover:bg-gray-900/50'}">
				<div class="flex-shrink-0 w-14 text-right pt-0.5">
					<span class="text-gray-500 font-mono text-xs">{formatTime(segment.start)}</span>
				</div>
				<div class="flex-shrink-0 w-28">
					{#if canEdit}
						<select
							value={segment.speaker}
							onchange={(e) => changeSpeaker(idx, (e.currentTarget as HTMLSelectElement).value)}
							disabled={busy}
							class="bg-transparent border border-transparent hover:border-gray-700 rounded px-1 py-0.5 text-xs font-medium max-w-full {transcriptState.getSpeakerColor(segment.speaker)}"
						>
							{#each sessionState.activeSession?.participants ?? [] as p}
								<option value={p}>{p}</option>
							{/each}
							{#if !(sessionState.activeSession?.participants ?? []).includes(segment.speaker)}
								<option value={segment.speaker}>{segment.speaker}</option>
							{/if}
							<option value="__new__">New speaker…</option>
						</select>
					{:else}
						<span class="font-medium {transcriptState.getSpeakerColor(segment.speaker)}">{segment.speaker}</span>
					{/if}
				</div>
				<div class="flex-1 text-gray-200 min-w-0">
					{#if editingIdx === idx}
						<textarea
							bind:this={textarea}
							bind:value={draft}
							onkeydown={onKey}
							onblur={saveEdit}
							rows={Math.max(2, Math.ceil(draft.length / 80))}
							class="w-full bg-gray-800 border border-gray-600 rounded px-2 py-1 text-sm text-gray-100 resize-y focus:outline-none"
						></textarea>
						<div class="flex gap-3 text-[11px] text-gray-500 mt-1">
							<button onmousedown={(e) => e.preventDefault()} onclick={saveEdit} class="hover:text-gray-200">Save</button>
							<button onmousedown={(e) => e.preventDefault()} onclick={splitAtCursor} class="hover:text-gray-200" title="Split this segment at the cursor position">Split at cursor</button>
							<button onmousedown={(e) => e.preventDefault()} onclick={() => (editingIdx = null)} class="hover:text-gray-200">Cancel</button>
						</div>
					{:else}
						{#if canEdit}
							<span
								role="button"
								tabindex="0"
								onclick={() => startEdit(idx)}
								onkeydown={(e) => { if (e.key === 'Enter') startEdit(idx); }}
								class="cursor-text focus:outline-none focus:ring-1 focus:ring-gray-600 rounded"
							>{segment.text}</span>
						{:else}
							<span>{segment.text}</span>
						{/if}
					{/if}
				</div>
				{#if canEdit && editingIdx !== idx}
					<div class="flex-shrink-0 flex items-start gap-1 opacity-0 group-hover:opacity-100 transition-opacity text-xs text-gray-500">
						{#if idx > 0}
							<button onclick={() => mergeUp(idx)} disabled={busy} title="Merge into previous segment" class="px-1 hover:text-gray-200">⤒</button>
						{/if}
						<button onclick={() => remove(idx)} disabled={busy} title="Delete segment" class="px-1 hover:text-red-400">✕</button>
					</div>
				{/if}
			</div>
		{/each}

		{#if transcriptState.segments.length === 0 && !transcriptState.isProcessing}
			<p class="text-gray-500 text-sm text-center py-8">
				{#if sessionState.activeSession?.audio_file}
					No transcript yet. Click Transcribe to process the recorded audio.
				{:else}
					No transcript yet. Record audio and it will be transcribed automatically.
				{/if}
			</p>
		{/if}
	</div>
</div>
