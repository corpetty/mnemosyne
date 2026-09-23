<script lang="ts">
	import { deleteSessionAudio, getStorage, runCleanup, updateSettings } from '$lib/api/backend.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { CleanupResult, StorageReport } from '$lib/types/index.js';

	let { locked = false }: { locked?: boolean } = $props();

	let report = $state<StorageReport | null>(null);
	let days = $state(0);
	let preview = $state<CleanupResult | null>(null);
	let busy = $state(false);

	function fmtBytes(n: number): string {
		if (n < 1024) return `${n} B`;
		const units = ['KB', 'MB', 'GB', 'TB'];
		let v = n / 1024;
		let i = 0;
		while (v >= 1024 && i < units.length - 1) {
			v /= 1024;
			i++;
		}
		return `${v.toFixed(v >= 10 ? 0 : 1)} ${units[i]}`;
	}

	function fmtDate(d: string): string {
		return new Date(d).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
	}

	async function load() {
		try {
			report = await getStorage();
			days = report.retention_days;
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not read storage');
		}
	}

	$effect(() => {
		load();
	});

	async function saveRetention() {
		busy = true;
		try {
			await updateSettings({ audio_retention_days: Math.max(0, Math.floor(days)) });
			preview = null;
			await load();
			toastState.success(days > 0 ? `Audio older than ${days} days will be removed after transcription` : 'Audio is kept forever');
		} finally {
			busy = false;
		}
	}

	async function previewCleanup() {
		if (days <= 0) return;
		preview = await runCleanup(days, true);
	}

	async function applyCleanup() {
		if (!preview || days <= 0) return;
		busy = true;
		try {
			const r = await runCleanup(days, false);
			toastState.success(`Freed ${fmtBytes(r.freed_bytes)} from ${r.sessions.length} session(s)`);
			preview = null;
			await load();
			await sessionState.loadSessions();
		} finally {
			busy = false;
		}
	}

	async function deleteAudio(id: string, name: string) {
		if (!confirm(`Delete the audio for "${name}"? The transcript, summary and notes are kept.`)) return;
		busy = true;
		try {
			await deleteSessionAudio(id);
			await load();
			await sessionState.loadSessions();
			if (sessionState.activeSession?.id === id) await sessionState.refreshActive();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not delete audio');
		} finally {
			busy = false;
		}
	}
</script>

<section>
	<h3 class="text-lg font-semibold text-gray-200 mb-1">Storage</h3>
	{#if report}
		<p class="text-xs text-gray-500 mb-3">
			Audio {fmtBytes(report.recordings_bytes)} across {report.sessions_with_audio} session{report.sessions_with_audio !== 1 ? 's' : ''} ·
			database {fmtBytes(report.database_bytes)} · <code class="text-gray-600">{report.data_dir}</code>
		</p>

		<div class="flex flex-wrap items-end gap-3">
			<label>
				<span class="block text-xs text-gray-500 mb-1">Keep audio for (days, 0 = forever)</span>
				<input
					type="number"
					min="0"
					step="1"
					bind:value={days}
					disabled={locked || busy}
					oninput={() => (preview = null)}
					class="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 w-32 disabled:opacity-60"
				/>
			</label>
			<button onclick={saveRetention} disabled={locked || busy || days === report.retention_days} class="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 disabled:opacity-50">
				Save
			</button>
			<button onclick={previewCleanup} disabled={busy || days <= 0} class="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 disabled:opacity-50">
				Preview clean-up
			</button>
		</div>
		<p class="text-[11px] text-gray-600 mt-1">
			Only audio is removed, and only from sessions that already have a transcript. Transcripts, summaries and notes are never deleted. Runs every 6 hours.
		</p>

		{#if preview}
			<div class="mt-3 rounded-lg border border-gray-800 bg-gray-900/60 p-3 text-sm">
				{#if preview.sessions.length === 0}
					<p class="text-gray-400">Nothing to remove with a {days}-day limit.</p>
				{:else}
					<p class="text-gray-300">
						Would free <span class="font-medium">{fmtBytes(preview.freed_bytes)}</span> from {preview.sessions.length} session{preview.sessions.length !== 1 ? 's' : ''}:
					</p>
					<ul class="mt-1 text-xs text-gray-500 max-h-32 overflow-y-auto">
						{#each preview.sessions as u (u.session_id)}
							<li>{u.name} · {fmtDate(u.created_at)} · {fmtBytes(u.audio_bytes)}</li>
						{/each}
					</ul>
					<button onclick={applyCleanup} disabled={busy} class="mt-2 px-3 py-1.5 text-sm rounded bg-red-900/60 hover:bg-red-800 border border-red-800 text-red-100 disabled:opacity-50">
						Delete this audio now
					</button>
				{/if}
			</div>
		{/if}

		{#if report.largest.length}
			<h4 class="text-sm font-semibold text-gray-300 mt-4 mb-1">Largest recordings</h4>
			<div class="space-y-1">
				{#each report.largest as u (u.session_id)}
					<div class="flex items-center gap-3 text-xs rounded px-2 py-1 hover:bg-gray-900">
						<span class="flex-1 text-gray-300 truncate">{u.name}</span>
						<span class="text-gray-600">{fmtDate(u.created_at)}</span>
						<span class="w-16 text-right text-gray-400 font-mono">{fmtBytes(u.audio_bytes)}</span>
						{#if !u.has_transcript}
							<span class="text-yellow-600" title="Not transcribed yet">untranscribed</span>
						{/if}
						<button onclick={() => deleteAudio(u.session_id, u.name)} disabled={busy} class="text-gray-500 hover:text-red-400 disabled:opacity-50">
							delete audio
						</button>
					</div>
				{/each}
			</div>
		{/if}
	{:else}
		<p class="text-xs text-gray-600">Loading…</p>
	{/if}
</section>
