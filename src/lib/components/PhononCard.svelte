<script lang="ts">
	import { onMount } from 'svelte';
	import { getSystemInfo, installPhonon, removePhonon } from '$lib/api/backend.js';
	import { jobsState } from '$lib/stores/jobs.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { SystemInfo } from '$lib/types/index.js';

	// Phonon-2 (English) as Mnemosyne installs it (backend services/phonon_install.py): its own
	// environment and model under the data folder, about 1 GB, removable as a whole.
	let system = $state<SystemInfo | null>(null);
	let jobId = $state<string | null>(null);
	let removing = $state(false);

	async function refresh() {
		system = await getSystemInfo().catch(() => null);
	}
	onMount(refresh);

	const job = $derived(
		jobId
			? (jobsState.jobs[jobId] ?? null)
			: (Object.values(jobsState.jobs).find(
					(j) => j.kind === 'install_phonon' && (j.status === 'queued' || j.status === 'running')
				) ?? null)
	);
	const installing = $derived(!!job && (job.status === 'queued' || job.status === 'running'));
	$effect(() => {
		if (job?.status === 'completed') {
			jobId = null;
			void refresh();
		}
	});
	const mb = (bytes: number) => `${Math.round(bytes / 1e6)} MB`;

	async function install() {
		try {
			const j = await installPhonon();
			jobsState.track(j);
			jobId = j.id;
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not start the install');
		}
	}

	async function remove() {
		if (!confirm('Remove Phonon-2? Transcription set to it uses Parakeet until it is installed again.')) return;
		removing = true;
		try {
			await removePhonon();
			await refresh();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not remove it');
		} finally {
			removing = false;
		}
	}
</script>

{#if system}
	<section aria-label="Phonon-2" class="col-span-2 rounded border border-gray-800 bg-gray-900 p-3 text-sm space-y-2">
		<h4 class="font-semibold text-gray-200">Phonon-2 {system.phonon ? '· installed' : ''}</h4>
		<p class="text-xs text-gray-400">
			For English meetings: Parakeet TDT 0.6B v3 in a 164 MB file with its own CPU engine, from Fermion Research (model
			CC BY 4.0). On AMI meetings it made fewer errors than Parakeet and ran about four times faster. Mnemosyne installs it
			in a folder of its own (about 1 GB with CPU PyTorch) and uses it when the language is English or it is chosen here.
		</p>
		{#if installing}
			<p class="text-xs text-gray-300">
				<span class="inline-block w-2 h-2 rounded-full bg-blue-500 animate-pulse mr-1"></span>
				{job?.message || 'Installing…'}
				{#if job?.progress}({Math.round(job.progress * 100)} %){/if}
			</p>
		{:else if system.phonon}
			<div class="flex items-center gap-3">
				<span class="text-xs text-gray-500">{mb(system.phonon_bytes)} on disk</span>
				<button onclick={remove} disabled={removing} class="px-3 py-1 text-xs rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 disabled:opacity-50">Remove</button>
			</div>
		{:else if system.phonon_installable}
			{#if job?.status === 'failed'}<p class="text-xs text-red-400">{job.error}</p>{/if}
			<button onclick={install} class="px-3 py-1.5 rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200">Install Phonon-2</button>
		{:else}
			<p class="text-xs text-gray-500">Installing it needs uv, which this backend does not have; install it yourself (below).</p>
		{/if}
	</section>
{/if}
