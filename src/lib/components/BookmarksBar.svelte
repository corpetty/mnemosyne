<script lang="ts">
	import { deleteBookmark, updateBookmark } from '$lib/api/backend.js';
	import { playerState } from '$lib/stores/player.svelte.js';
	import { canChange } from '$lib/app/access.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';

	// A meeting's bookmarks: moments marked while recording (Mark, Ctrl+M, tray) or on lines.
	const session = $derived(sessionState.activeSession);
	const marks = $derived(session?.bookmarks ?? []);

	const clock = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;

	function open(at: number) {
		if (!session) return;
		const idx = session.transcript.findLastIndex((seg) => seg.start <= at + 0.5);
		sessionState.pendingOpen = { sessionId: session.id, idx: idx < 0 ? 0 : idx };
		if (playerState.available) playerState.seek(at, false);
	}

	async function edit(id: string, note: string) {
		if (!session) return;
		const next = prompt('Note for this bookmark', note);
		if (next === null) return;
		try {
			await updateBookmark(session.id, id, next);
			await sessionState.refreshActive();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not save the note');
		}
	}

	async function remove(id: string) {
		if (!session) return;
		try {
			await deleteBookmark(session.id, id);
			await sessionState.refreshActive();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not remove the bookmark');
		}
	}
</script>

{#if marks.length}
	<div class="mb-3 flex flex-wrap items-center gap-2" aria-label="Bookmarks">
		<span class="text-xs text-gray-500">Bookmarks</span>
		{#each marks as b (b.id)}
			<span class="group flex items-center gap-1 rounded-full border border-amber-800/70 bg-amber-950/30 pl-2 pr-1 py-0.5 text-xs text-amber-200">
				<button onclick={() => open(b.at)} class="flex items-center gap-1 hover:text-amber-100" title="Go to this moment">
					🔖 <span class="font-mono">{clock(b.at)}</span>{#if b.note}<span class="text-amber-100/90">{b.note}</span>{/if}
				</button>
				{#if canChange(sessionState.activeSession)}
				<button onclick={() => edit(b.id, b.note)} class="px-1 text-amber-500/70 hover:text-amber-200" title="Edit the note" aria-label="Edit the note">✎</button>
				<button onclick={() => remove(b.id)} class="px-1 text-amber-500/70 hover:text-amber-200" title="Remove" aria-label="Remove the bookmark">✕</button>
				{/if}
			</span>
		{/each}
	</div>
{/if}
