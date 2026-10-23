<script lang="ts">
	import { addBookmark, clipUrl, makeQuote, saveClip } from '$lib/api/backend.js';
	import { commentsState } from '$lib/stores/comments.svelte.js';
	import { connectionState } from '$lib/stores/connection.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { publishingState } from '$lib/stores/publishing.svelte.js';
	import { canChange } from '$lib/app/access.js';

	let {
		from,
		to,
		onclose,
		oncomment
	}: { from: number; to: number; onclose: () => void; oncomment?: () => void } = $props();
	let busy = $state<'text' | 'audio' | null>(null);

	const session = $derived(sessionState.activeSession);
	const span = $derived.by(() => {
		const segs = session?.transcript ?? [];
		return segs.length ? { start: segs[from]?.start ?? 0, end: segs[to]?.end ?? 0 } : null;
	});

	function fmt(t: number): string {
		return `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}`;
	}

	/** Bookmark the first selected line. */
	async function bookmark() {
		const s = sessionState.activeSession;
		if (!s) return;
		try {
			await addBookmark(s.id, s.transcript[from].start);
			await sessionState.refreshActive();
			toastState.success('Bookmarked');
			onclose();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not bookmark');
		}
	}

	/** Keep the selected lines for the meeting's publication (the Publish dialog lists them). */
	function addToPublication() {
		const s = sessionState.activeSession;
		if (!s || !span) return;
		const n = publishingState.addPending(s.id, { start: span.start, end: span.end, part: null, with_clip: false });
		toastState.show(`Added to the publication (${n} quote${n === 1 ? '' : 's'})`, 'success', 6000, {
			label: 'Open',
			run: () => {
				const now = sessionState.activeSession;
				if (now?.id === s.id) void publishingState.open(now);
			}
		});
		onclose();
	}

	async function copyText() {
		if (!session) return;
		busy = 'text';
		try {
			const q = await makeQuote(session.id, from, to, false);
			await navigator.clipboard.writeText(q.text);
			toastState.success('Quote copied');
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not copy the quote');
		} finally {
			busy = null;
		}
	}

	async function saveAudio() {
		if (!session) return;
		busy = 'audio';
		try {
			const q = await makeQuote(session.id, from, to, true);
			if (!q.clip) throw new Error('This meeting has no audio');
			const { isTauri } = await import('@tauri-apps/api/core');
			if (isTauri() && connectionState.isLocal) {
				const { save } = await import('@tauri-apps/plugin-dialog');
				const path = await save({ defaultPath: q.clip.filename, filters: [{ name: 'Opus audio', extensions: ['ogg'] }] });
				if (!path) return;
				await saveClip(session.id, q.clip.id, path);
				toastState.success(`Saved ${q.clip.seconds} s of audio`);
			} else {
				const blob = await (await fetch(clipUrl(session.id, q.clip.id))).blob();
				const a = document.createElement('a');
				a.href = URL.createObjectURL(blob);
				a.download = q.clip.filename;
				a.click();
				URL.revokeObjectURL(a.href);
			}
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not save the clip');
		} finally {
			busy = null;
		}
	}
</script>

<div class="sticky top-0 z-10 flex flex-wrap items-center gap-2 rounded border border-amber-800/60 bg-gray-900/95 px-2 py-1.5 text-xs" role="toolbar" aria-label="Quote">
	<span class="text-amber-300">❝</span>
	<span class="text-gray-300">
		{to - from + 1} line{to > from ? 's' : ''}{span ? ` · ${fmt(span.start)}–${fmt(span.end)}` : ''}
	</span>
	<span class="text-gray-600">shift-click ❝ on another line to extend</span>
	<button onclick={copyText} disabled={!!busy} class="ml-auto px-2 py-0.5 rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200 disabled:opacity-50">
		{busy === 'text' ? 'Copying…' : 'Copy text'}
	</button>
	{#if session?.audio_file}
		<button onclick={saveAudio} disabled={!!busy} class="px-2 py-0.5 rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200 disabled:opacity-50">
			{busy === 'audio' ? 'Cutting…' : 'Save audio clip'}
		</button>
	{/if}
	{#if publishingState.allowed && canChange(session) && span}
		<button onclick={addToPublication} disabled={!!busy} class="px-2 py-0.5 rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200 disabled:opacity-50">
			Add to publication
		</button>
	{/if}
	{#if commentsState.canComment && oncomment}
		<button onclick={oncomment} disabled={!!busy} class="px-2 py-0.5 rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-sky-300 disabled:opacity-50">
			Comment
		</button>
	{/if}
	<button onclick={bookmark} disabled={!!busy} class="px-2 py-0.5 rounded bg-amber-950/60 hover:bg-amber-900/60 border border-amber-800/70 text-amber-200 disabled:opacity-50">
		🔖 Bookmark
	</button>
	<button onclick={onclose} aria-label="Cancel the quote" class="px-1 text-gray-500 hover:text-gray-200">✕</button>
</div>
