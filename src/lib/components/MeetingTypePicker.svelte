<script lang="ts">
	import { getSettings, setMeetingType } from '$lib/api/backend.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';

	// The open meeting's type (Settings → AI → Meeting types, plus the advisor pack when it is
	// on); shown once any type exists.
	let names = $state<string[]>([]);
	$effect(() => {
		getSettings()
			.then((s) => (names = s.meeting_type_names))
			.catch(() => {});
	});
	const session = $derived(sessionState.activeSession);
	const current = $derived(session?.meeting_type && session.meeting_type !== 'none' ? session.meeting_type : 'none');

	async function choose(e: Event) {
		if (!session) return;
		const name = (e.currentTarget as HTMLSelectElement).value;
		try {
			sessionState.update(await setMeetingType(session.id, name));
			await sessionState.loadSessions();
		} catch (err) {
			toastState.error(err instanceof Error ? err.message : 'Could not set the type');
		}
	}
</script>

{#if session && names.length}
	<select
		value={current}
		onchange={choose}
		aria-label="Meeting type"
		title="Meeting type: its summary style, instructions and Obsidian folder"
		class="bg-transparent border border-gray-800 rounded px-1.5 py-0.5 text-xs text-gray-400 hover:text-gray-200"
	>
		<option value="none">No type</option>
		{#each names as name (name)}<option value={name}>{name}</option>{/each}
	</select>
{/if}
