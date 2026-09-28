<script lang="ts">
	import { backUpNow, getBackups, getJob, getSettings, restoreBackup, updateSettings } from '$lib/api/backend.js';
	import { restartBackendForRestore } from '$lib/app/controller.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { BackupStatus } from '$lib/types/index.js';

	// Backups of meetings, audio and settings (backend services/backup.py), and restoring one.
	let status = $state<BackupStatus | null>(null);
	let dir = $state('');
	let interval = $state(0);
	let keep = $state(3);
	let saved = $state({ dir: '', interval: 0, keep: 3 });
	let progress = $state<number | null>(null); // a backup being made
	let busy = $state(false);

	const INTERVALS = [
		{ days: 0, label: 'Only when I ask' },
		{ days: 1, label: 'Every day' },
		{ days: 7, label: 'Every week' },
		{ days: 30, label: 'Every month' }
	];

	function fmtBytes(n: number): string {
		const units = ['B', 'KB', 'MB', 'GB', 'TB'];
		let v = n;
		let i = 0;
		while (v >= 1024 && i < units.length - 1) {
			v /= 1024;
			i++;
		}
		return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
	}

	const fmtWhen = (d: string) =>
		new Date(d).toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });

	async function load() {
		try {
			const [st, settings] = await Promise.all([getBackups(), getSettings()]);
			status = st;
			dir = settings.values.backup_dir ?? '';
			interval = settings.values.backup_interval_days ?? 0;
			keep = settings.values.backup_keep ?? 3;
			saved = { dir, interval, keep };
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not read backups');
		}
	}

	$effect(() => {
		load();
	});

	const changed = $derived(dir !== saved.dir || interval !== saved.interval || keep !== saved.keep);

	async function save() {
		busy = true;
		try {
			await updateSettings({ backup_dir: dir.trim(), backup_interval_days: interval, backup_keep: Math.max(1, Math.floor(keep)) });
			await load();
			toastState.success('Backup settings saved');
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not save');
		} finally {
			busy = false;
		}
	}

	async function backUp() {
		busy = true;
		progress = 0;
		try {
			const job = await backUpNow();
			for (;;) {
				const j = await getJob(job.id);
				progress = j.progress ?? 0;
				if (j.status === 'completed') {
					toastState.success('Backup made');
					break;
				}
				if (j.status === 'failed' || j.status === 'cancelled') {
					toastState.error(j.error ?? 'The backup failed');
					break;
				}
				await new Promise((r) => setTimeout(r, 700));
			}
			await load();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not back up');
		} finally {
			busy = false;
			progress = null;
		}
	}

	async function restore(name: string, when: string) {
		const ok = confirm(
			`Restore the backup from ${fmtWhen(when)}?\n\nMeetings, audio and settings go back to how they were then. ` +
				'What is here now is not deleted: it is kept in a "pre-restore" folder in the data folder. Mnemosyne restarts to do this.'
		);
		if (!ok) return;
		busy = true;
		try {
			await restoreBackup(name);
			toastState.show('Restoring: the backend restarts…', 'info', 20_000);
			if (!(await restartBackendForRestore())) {
				await load();
				toastState.show('Restart the backend to finish restoring', 'info', 20_000);
			}
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not restore');
		} finally {
			busy = false;
		}
	}
</script>

<section>
	<h3 class="text-lg font-semibold text-gray-200 mb-1">Backups</h3>
	<p class="text-xs text-gray-500 mb-3">
		One file with every meeting, its audio and the settings (not API keys or tokens), to restore here or on a new
		computer. Keep backups on another disk or synced storage.
	</p>
	{#if status}
		{#if status.restore_pending}
			<p class="mb-3 rounded border border-amber-800 bg-amber-950/40 px-3 py-2 text-sm text-amber-200">
				{status.restore_pending} is restored when the backend restarts.
			</p>
		{/if}
		{#if status.last_restore}
			<p class="mb-3 text-xs {status.last_restore.error ? 'text-red-400' : 'text-gray-500'}">
				{#if status.last_restore.error}
					Restoring {status.last_restore.name} failed ({status.last_restore.error}); nothing was changed.
				{:else}
					Restored {status.last_restore.name} on {fmtWhen(status.last_restore.at)}. What was here before is in
					<code class="text-gray-400">{status.last_restore.kept_in}</code>.
				{/if}
			</p>
		{/if}

		<div class="flex flex-wrap items-end gap-3">
			<label class="grow min-w-64">
				<span class="block text-xs text-gray-500 mb-1">Folder</span>
				<input
					bind:value={dir}
					placeholder={status.dir}
					disabled={busy}
					class="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 disabled:opacity-60"
				/>
			</label>
			<label>
				<span class="block text-xs text-gray-500 mb-1">Back up</span>
				<select bind:value={interval} disabled={busy} class="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200">
					{#each INTERVALS as o (o.days)}
						<option value={o.days}>{o.label}</option>
					{/each}
				</select>
			</label>
			<label>
				<span class="block text-xs text-gray-500 mb-1">Keep</span>
				<input
					type="number"
					min="1"
					step="1"
					bind:value={keep}
					disabled={busy}
					class="w-20 bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200"
				/>
			</label>
			<button onclick={save} disabled={busy || !changed} class="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 disabled:opacity-50">
				Save
			</button>
			<button onclick={backUp} disabled={busy} class="px-3 py-1.5 text-sm rounded bg-emerald-800 hover:bg-emerald-700 text-white disabled:opacity-50">
				{progress !== null ? `Backing up… ${Math.round(progress * 100)}%` : 'Back up now'}
			</button>
		</div>
		<p class="text-[11px] text-gray-600 mt-1">
			Automatic backups are skipped while recording; older ones beyond the number to keep are removed.
		</p>

		{#if status.backups.length}
			<ul class="mt-3 divide-y divide-gray-800 rounded-lg border border-gray-800" aria-label="Backups">
				{#each status.backups as b (b.name)}
					<li class="flex items-center gap-3 px-3 py-2 text-sm">
						<span class="text-gray-200">{fmtWhen(b.created_at)}</span>
						<span class="text-xs text-gray-500">
							{b.sessions} meeting{b.sessions !== 1 ? 's' : ''} · {fmtBytes(b.size)} · {b.encrypted ? 'encrypted' : 'not encrypted'}
						</span>
						<button onclick={() => restore(b.name, b.created_at)} disabled={busy} class="ml-auto text-xs text-gray-400 hover:text-gray-200 disabled:opacity-50">
							Restore
						</button>
					</li>
				{/each}
			</ul>
			{#if status.backups.some((b) => !b.encrypted)}
				<p class="text-[11px] text-gray-600 mt-1">
					A backup that is not encrypted can be read by anyone who gets the file. Turn on encryption (on this tab) to make
					new backups encrypted; they then open with your recovery code.
				</p>
			{/if}
		{:else}
			<p class="mt-3 text-xs text-gray-500">No backups in this folder yet.</p>
		{/if}
	{/if}
</section>
