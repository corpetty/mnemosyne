<script lang="ts">
	import type { Job } from '$lib/types/index.js';

	let { job, compact = false }: { job: Job; compact?: boolean } = $props();

	let now = $state(Date.now());
	$effect(() => {
		const t = setInterval(() => (now = Date.now()), 1000);
		return () => clearInterval(t);
	});

	const pct = $derived(Math.round((job.progress ?? 0) * 100));
	const eta = $derived.by(() => {
		const p = job.progress ?? 0;
		if (!job.started_at || p < 0.05 || job.status !== 'running') return null;
		const elapsed = (now - new Date(job.started_at).getTime()) / 1000;
		const left = Math.max(0, (elapsed * (1 - p)) / p);
		if (left < 60) return `${Math.ceil(left / 5) * 5} s left`;
		return `${Math.round(left / 60)} min left`;
	});
</script>

{#if compact}
	<div class="h-1 w-full rounded-full bg-gray-800 overflow-hidden" title="{job.message} {pct}%">
		<div class="h-full bg-yellow-500 transition-[width] duration-500" style="width: {Math.max(pct, 3)}%"></div>
	</div>
{:else}
	<div class="space-y-1">
		<div class="flex justify-between text-xs text-gray-400">
			<span>{job.status === 'queued' ? 'Waiting for another transcription to finish…' : job.message || 'Working…'}</span>
			<span class="font-mono text-gray-500">{pct}%{#if eta}&nbsp;· {eta}{/if}</span>
		</div>
		<div class="h-1.5 w-full rounded-full bg-gray-800 overflow-hidden">
			<div class="h-full bg-yellow-500 transition-[width] duration-500" style="width: {Math.max(pct, 2)}%"></div>
		</div>
	</div>
{/if}
