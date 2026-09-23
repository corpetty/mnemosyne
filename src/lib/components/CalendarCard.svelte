<script lang="ts">
	import { calendarState, fmtClock, fmtRelative } from '$lib/stores/calendar.svelte.js';

	const next = $derived(
		calendarState.upcoming.filter((e) => e.uid !== calendarState.current?.uid && new Date(e.start).getTime() > calendarState.now).slice(0, 3)
	);
</script>

{#if calendarState.configured}
	<div class="rounded-lg border border-gray-800 bg-gray-900/60 px-3 py-2 text-xs space-y-1">
		{#if calendarState.current}
			<p class="text-gray-300">
				<span class="text-green-400 font-medium">Now:</span>
				{calendarState.current.title}
				<span class="text-gray-600">· until {fmtClock(calendarState.current.end)}{#if calendarState.current.attendees.length}&nbsp;· {calendarState.current.attendees.length} invited{/if}</span>
			</p>
			<p class="text-gray-600">A new recording will be named after this meeting.</p>
		{/if}
		{#each next as e (e.uid)}
			<p class="text-gray-500">
				<span class="text-gray-400">Next:</span> {e.title}
				<span class="text-gray-600">· {fmtClock(e.start)} ({fmtRelative(e.start, calendarState.now)})</span>
			</p>
		{/each}
		{#if !calendarState.current && next.length === 0}
			<p class="text-gray-600">No meetings in the next 12 hours.</p>
		{/if}
		{#if calendarState.error}
			<p class="text-yellow-600">Calendar: {calendarState.error}</p>
		{/if}
	</div>
{/if}
