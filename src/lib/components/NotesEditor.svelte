<script lang="ts">
	import { sessionState } from '$lib/stores/session.svelte.js';

	let saveTimer: ReturnType<typeof setTimeout> | null = null;
	let localNotes = $state('');
	let lastSavedSessionId = $state<string | null>(null);

	// Sync local notes when active session changes
	$effect(() => {
		const session = sessionState.activeSession;
		if (session && session.id !== lastSavedSessionId) {
			localNotes = session.notes;
			lastSavedSessionId = session.id;
		}
	});

	function handleInput(e: Event) {
		const target = e.target as HTMLTextAreaElement;
		localNotes = target.value;

		// Auto-save after 1 second of inactivity
		if (saveTimer) clearTimeout(saveTimer);
		saveTimer = setTimeout(() => {
			const session = sessionState.activeSession;
			if (session) {
				sessionState.updateNotes(session.id, localNotes);
			}
		}, 1000);
	}
</script>

<div>
	<textarea
		value={localNotes}
		oninput={handleInput}
		placeholder="Add notes about this session..."
		class="w-full h-40 px-3 py-2 text-sm bg-gray-900 border border-gray-700 rounded-lg text-gray-200 placeholder-gray-600 resize-y focus:outline-none focus:border-gray-500"
	></textarea>
</div>
