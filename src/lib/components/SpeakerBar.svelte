<script lang="ts">
	import { getSessionSpeakers, renameSessionSpeaker } from '$lib/api/backend.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { transcriptState } from '$lib/stores/transcript.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { SessionSpeaker } from '$lib/types/index.js';

	let speakers = $state<SessionSpeaker[]>([]);
	let editing = $state<string | null>(null);
	let draft = $state('');
	let remember = $state(true);
	let loadedFor = $state<string | null>(null);

	async function load(sessionId: string) {
		try {
			speakers = await getSessionSpeakers(sessionId);
			loadedFor = sessionId;
		} catch {
			speakers = [];
		}
	}

	$effect(() => {
		const session = sessionState.activeSession;
		if (!session) return;
		// Reload when the session or its participant list changes.
		const key = session.id + '|' + session.participants.join(',');
		if (key !== loadedFor) {
			loadedFor = key;
			load(session.id);
		}
	});

	function startEdit(s: SessionSpeaker) {
		editing = s.label;
		draft = s.label.startsWith('SPEAKER_') ? '' : s.label;
		remember = s.has_voice;
	}

	async function commit() {
		const session = sessionState.activeSession;
		if (!session || !editing) return;
		const label = editing;
		const name = draft.trim();
		editing = null;
		if (!name || name === label) return;
		try {
			const updated = await renameSessionSpeaker(session.id, label, name, remember);
			sessionState.activeSession = updated;
			transcriptState.showSession(updated.id, updated.transcript);
			toastState.success(remember ? `${name} saved; will be recognized next time` : `Renamed to ${name}`);
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Rename failed');
		}
	}
</script>

{#if speakers.length > 0}
	<div class="flex flex-wrap items-center gap-2 text-xs">
		<span class="text-gray-600">Speakers:</span>
		{#each speakers as s (s.label)}
			{#if editing === s.label}
				<form onsubmit={(e) => { e.preventDefault(); commit(); }} class="flex items-center gap-1.5 bg-gray-900 border border-gray-700 rounded px-2 py-1">
					<!-- svelte-ignore a11y_autofocus -->
					<input
						bind:value={draft}
						placeholder="Name"
						autofocus
						onkeydown={(e) => { if (e.key === 'Escape') editing = null; }}
						class="bg-gray-800 border border-gray-700 rounded px-1.5 py-0.5 text-xs text-gray-200 w-32"
					/>
					{#if s.has_voice}
						<label class="flex items-center gap-1 text-gray-500" title="Store this voice so the person is labelled automatically in future sessions">
							<input type="checkbox" bind:checked={remember} class="rounded border-gray-600 bg-gray-800" /> remember voice
						</label>
					{/if}
					<button type="submit" class="text-blue-400 hover:text-blue-300">Save</button>
					<button type="button" onclick={() => (editing = null)} class="text-gray-500 hover:text-gray-300">Cancel</button>
				</form>
			{:else}
				<button
					onclick={() => startEdit(s)}
					title="Rename this speaker"
					class="px-2 py-0.5 rounded border border-gray-800 bg-gray-900 hover:border-gray-600 font-medium {transcriptState.getSpeakerColor(s.label)}"
				>
					{s.label}{#if s.has_voice}<span class="ml-1 text-gray-600" title="voice embedding available">●</span>{/if}
				</button>
			{/if}
		{/each}
	</div>
{/if}
