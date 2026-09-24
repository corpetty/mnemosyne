<script lang="ts">
	import { handleRemoteAction } from '$lib/app/controller.svelte.js';
	import { audioState } from '$lib/stores/audio.svelte.js';
	import { calendarState } from '$lib/stores/calendar.svelte.js';
	import MeetingBrief from './MeetingBrief.svelte';
</script>

{#if calendarState.starting && !audioState.isRecording}
	{@const ev = calendarState.starting}
	<div class="flex items-center justify-between gap-3 border-b border-blue-900 bg-blue-950/50 px-4 py-2 text-sm flex-shrink-0">
		<span class="text-blue-100"><span class="font-medium">{ev.title}</span> is starting{#if ev.attendees.length}<span class="text-blue-300">&nbsp;· {ev.attendees.length} invited</span>{/if}</span>
		<div class="flex items-center gap-2">
			<button onclick={() => { calendarState.dismiss(ev.uid); handleRemoteAction('start-record'); }} class="px-3 py-1 rounded bg-red-600 hover:bg-red-700 text-white text-xs font-medium">Record</button>
			<button onclick={() => calendarState.dismiss(ev.uid)} class="text-xs text-blue-300 hover:text-blue-100">Dismiss</button>
		</div>
	</div>
	<div class="border-b border-blue-900 bg-blue-950/30 px-4 py-1.5 flex-shrink-0 empty:hidden">
		<MeetingBrief title={ev.title} attendees={ev.attendees} compact />
	</div>
{/if}
