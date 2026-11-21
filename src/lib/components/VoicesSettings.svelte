<script lang="ts">
	import { onMount } from 'svelte';
	import { createVoice, deleteVoice, getVoiceEngine, installVoiceEngine, listVoices, removeVoiceEngine } from '$lib/api/backend.js';
	import { plainError, waitForJob } from '$lib/app/voiceRecording.js';
	import { connectionState } from '$lib/stores/connection.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { EngineInfo, Job, Voice, VoiceEngine } from '$lib/types/index.js';
	import VoiceStudio from './VoiceStudio.svelte';

	// Settings -> Voices: the narration engines (Qwen3-TTS 1.7B recommended, KittenTTS 2 as a faster
	// choice), installed on request, and your narration voices (backend services/voices.py;
	// docs/plans/2026-11-20-narration-voices.md).
	let engine = $state<VoiceEngine | null>(null);
	let voices = $state<Voice[]>([]);
	let open = $state<string | null>(null);
	let accepted = $state<Record<string, boolean>>({});
	let install = $state<Job | null>(null);
	let installingName = $state('');
	let newName = $state('');
	let busy = $state(false);

	const installing = $derived(!!install && (install.status === 'queued' || install.status === 'running'));
	const isAdmin = $derived(!connectionState.me?.team_mode || connectionState.me?.role === 'admin');
	const installedCount = $derived(engine?.engines.filter((e) => e.installed).length ?? 0);
	const inUse = $derived(engine?.engines.find((e) => e.name === engine?.engine) ?? null);

	async function refresh() {
		engine = await getVoiceEngine().catch(() => null);
		voices = await listVoices().catch(() => []);
	}
	onMount(refresh);

	async function startInstall(e: EngineInfo) {
		try {
			installingName = e.name;
			install = await installVoiceEngine(e.name, !!accepted[e.name]);
			install = await waitForJob(install);
			if (install.status === 'failed') toastState.error(install.error || 'The install failed');
			await refresh();
		} catch (err) {
			toastState.error(plainError(err, 'Could not start the install'));
		}
	}

	async function remove(e: EngineInfo) {
		if (!confirm(`Remove ${e.title} and its model? Your voices and narrations stay.`)) return;
		await removeVoiceEngine(e.name).catch((err) => toastState.error(plainError(err, 'Could not remove it')));
		await refresh();
	}

	async function add() {
		if (!newName.trim()) return;
		busy = true;
		try {
			const v = await createVoice(newName.trim());
			newName = '';
			await refresh();
			open = v.id;
		} catch (e) {
			toastState.error(plainError(e, 'Could not make the voice'));
		} finally {
			busy = false;
		}
	}

	async function forget(v: Voice) {
		if (!confirm(`Delete the voice "${v.name}" and every recording of it? Narrations made with it stay.`)) return;
		await deleteVoice(v.id).catch((e) => toastState.error(plainError(e, 'Could not delete it')));
		if (open === v.id) open = null;
		await refresh();
	}

	const mb = (bytes: number) => `${Math.round(bytes / 1e6).toLocaleString()} MB`;
	function runsOn(platform: string): string {
		if (platform === 'linux-cuda') return 'on your NVIDIA GPU';
		if (platform === 'macos') return "on the Mac's GPU";
		return 'on the CPU, which is slow for this model';
	}
	const ABOUT: Record<string, string> = {
		qwen3:
			'From the Qwen team at Alibaba. In our tests it sounded most like the person and misread the fewest words. About two times slower than real time on an NVIDIA GPU, about seven times on a CPU.',
		kitten: 'From KittenML. About twice as fast as Qwen3-TTS, a little less like the person in our tests.'
	};
</script>

<div class="space-y-4">
	<section aria-label="Narration engine" class="rounded border border-gray-800 bg-gray-900 p-3 text-sm space-y-3">
		<h4 class="font-semibold text-gray-200">Narration engine {inUse ? `· using ${inUse.title}` : ''}</h4>
		<p class="text-xs text-gray-400">
			Mnemosyne can read summaries, digests and notes aloud in your own voice. It learns the voice from your recordings on
			this computer and never sends them anywhere. Each engine installs in a folder of its own.
		</p>
		{#if engine}
			{#each engine.engines as e (e.name)}
				<div class="rounded border {e.recommended ? 'border-blue-800/70' : 'border-gray-800'} p-2 space-y-1.5">
					<div class="flex items-center gap-2">
						<span class="text-gray-200">{e.title}</span>
						{#if e.recommended}<span class="text-[10px] px-1.5 rounded bg-blue-900/60 text-blue-200">recommended</span>{/if}
						{#if e.installed}<span class="text-[10px] px-1.5 rounded bg-green-900/50 text-green-300">installed</span>{/if}
						<span class="ml-auto text-[11px] text-gray-500">{e.license_name}</span>
					</div>
					<p class="text-xs text-gray-400">{ABOUT[e.name] ?? ''} Here it would run {runsOn(e.platform)}; {e.size_note}.</p>
					{#if installing && installingName === e.name}
						<p class="text-xs text-gray-300">
							<span class="inline-block w-2 h-2 rounded-full bg-blue-500 animate-pulse mr-1"></span>
							{install?.message || 'Installing…'}{#if install?.progress} ({Math.round(install.progress * 100)} %){/if}
						</p>
					{:else if e.installed}
						<div class="flex items-center gap-3">
							<span class="text-xs text-gray-500">{mb(e.install_bytes)} on disk{e.attribution ? ` · ${e.attribution}` : ''}</span>
							{#if isAdmin}<button onclick={() => remove(e)} class="px-3 py-1 text-xs rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300">Remove</button>{/if}
						</div>
					{:else if !isAdmin}
						<p class="text-xs text-gray-500">An admin installs narration engines.</p>
					{:else if !engine.installable}
						<p class="text-xs text-gray-500">Installing it needs uv, which this backend does not have.</p>
					{:else}
						{#if !e.open_license}
							<div class="rounded border border-amber-700/60 bg-amber-950/30 p-2 text-xs text-gray-300 space-y-2">
								<p>
									{e.title}'s model is not open source: it comes under the
									<a href={e.license_url} target="_blank" rel="noreferrer" class="underline">{e.license_name}</a>. It is free for
									people and organizations with less than US$1 million in yearly revenue and less than US$1 million in funding;
									commercial use must be registered with Stellon Labs, and its output may not be used to train other models.
								</p>
								<label class="flex items-center gap-2">
									<input type="checkbox" bind:checked={accepted[e.name]} />
									I have read and accept the {e.license_name}
								</label>
							</div>
						{/if}
						{#if install?.status === 'failed' && installingName === e.name}<p class="text-xs text-red-400">{install.error}</p>{/if}
						<button
							onclick={() => startInstall(e)}
							disabled={installing || (!e.open_license && !accepted[e.name])}
							class="px-3 py-1.5 rounded border disabled:opacity-50 {e.recommended ? 'bg-blue-700 hover:bg-blue-600 border-blue-600 text-white' : 'bg-gray-800 hover:bg-gray-700 border-gray-700 text-gray-200'}"
						>Install {e.title}</button>
					{/if}
				</div>
			{/each}
			{#if installedCount > 1}
				<p class="text-xs text-gray-500">With both installed, narration uses {inUse?.title}. Remove the other to switch.</p>
			{/if}
			{#if !engine.can_check_voices}
				<p class="text-xs text-amber-300">
					Making a voice needs the speaker model that recognizes voices: Settings → Transcription → Diarizer, On the CPU (ONNX).
				</p>
			{/if}
		{/if}
	</section>

	<section aria-label="Your voices" class="space-y-2">
		<h4 class="text-sm font-semibold text-gray-200">Your voices</h4>
		<p class="text-xs text-gray-500">
			A voice is made only from your own recordings, confirmed by reading a short statement aloud, live, in your voice.
			Only you can use your voices. Everything narrated is marked as synthetic, so Mnemosyne never mistakes it for you.
		</p>
		{#each voices as v (v.id)}
			<div class="rounded border border-gray-800 bg-gray-900/60">
				<div class="flex items-center gap-3 px-3 py-2 text-sm">
					<button onclick={() => (open = open === v.id ? null : v.id)} class="text-left text-gray-200 hover:text-white flex-1">
						{v.name}
						<span class="ml-2 text-xs {v.ready ? 'text-green-400' : 'text-gray-500'}">
							{v.ready ? 'ready' : `needs ${v.needs}`} · {Math.round(v.seconds)} s recorded
						</span>
					</button>
					<button onclick={() => forget(v)} class="text-xs text-gray-500 hover:text-red-400">Delete</button>
				</div>
				{#if open === v.id}
					<div class="border-t border-gray-800 p-3">
						<VoiceStudio voice={v} engineReady={installedCount > 0} onchange={refresh} />
					</div>
				{/if}
			</div>
		{/each}
		<div class="flex items-center gap-2">
			<input
				bind:value={newName}
				placeholder="Name, e.g. My voice"
				onkeydown={(e) => e.key === 'Enter' && add()}
				class="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 w-56"
			/>
			<button onclick={add} disabled={busy || !newName.trim()} class="px-3 py-1.5 text-sm rounded bg-blue-700 hover:bg-blue-600 text-white disabled:opacity-50">New voice</button>
		</div>
	</section>
</div>
