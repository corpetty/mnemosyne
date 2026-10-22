<script lang="ts">
	// The meeting's notes: the live shared editor, or the plain-text one when this backend has no
	// sidecar for live notes ("unavailable").
	import { acquireNotes, releaseNotes } from '$lib/stores/notes.svelte.js';
	import type { NotesDocState } from '$lib/notes/doc.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import LegacyNotesEditor from './LegacyNotesEditor.svelte';
	import SharedNotes from './SharedNotes.svelte';

	/** Classes that limit the editor's height and make it scroll (the recording tab's card). */
	let { maxHeight = '' }: { maxHeight?: string } = $props();

	const sessionId = $derived(sessionState.activeSession?.id ?? null);
	let docState = $state.raw<NotesDocState | null>(null);

	// The same reference SharedNotes takes: one connection, and it is already open when the
	// choice is made.
	$effect(() => {
		const id = sessionId;
		if (!id) {
			docState = null;
			return;
		}
		docState = acquireNotes(id);
		return () => releaseNotes(id);
	});
</script>

{#if docState?.status === 'unavailable'}
	<LegacyNotesEditor />
{:else if !docState || (docState.status === 'connecting' && !docState.doc)}
	<div class="space-y-2">
		<div class="flex items-center justify-end">
			<span class="text-[11px] text-gray-600" aria-live="polite">Connecting…</span>
		</div>
		<div class="min-h-64 px-4 py-3 bg-gray-900 border border-gray-700 rounded-lg {maxHeight}"></div>
	</div>
{:else}
	<SharedNotes {maxHeight} />
{/if}
