<script lang="ts">
	import type { Level } from '$lib/types/index.js';

	let { level, compact = false }: { level: Level | null | undefined; compact?: boolean } = $props();

	// -60 dBFS .. 0 dBFS mapped to 0..100 %
	const pct = (db: number) => Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
	const rms = $derived(level ? pct(level.rms_db) : 0);
	const peak = $derived(level ? pct(level.peak_db) : 0);
	const color = $derived(
		!level || level.rms_db < -55 ? 'bg-gray-600' : level.peak_db > -1 ? 'bg-red-500' : level.rms_db > -12 ? 'bg-yellow-500' : 'bg-green-500'
	);
	const title = $derived(
		level ? `RMS ${level.rms_db.toFixed(0)} dBFS · peak ${level.peak_db.toFixed(0)} dBFS` : 'No signal yet'
	);
</script>

<div class="relative {compact ? 'w-12 h-1.5' : 'w-24 h-2'} rounded-full bg-gray-800 overflow-hidden" {title}>
	<div class="absolute inset-y-0 left-0 {color} transition-[width] duration-150" style="width: {rms}%"></div>
	<div class="absolute inset-y-0 w-0.5 bg-gray-200/70" style="left: calc({peak}% - 1px)"></div>
</div>
