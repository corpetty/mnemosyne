<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		addVoiceAudio,
		correctVoiceClip,
		deleteVoiceClip,
		getConsentChallenge,
		getVoiceScript,
		narrate,
		narrationAudioUrl,
		sendConsent,
		setVoiceReference,
		voiceClipUrl
	} from '$lib/api/backend.js';
	import { VoiceRecorder, plainError, waitForJob } from '$lib/app/voiceRecording.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { ConsentChallenge, ConsentResult, Voice } from '$lib/types/index.js';

	// One narration voice: read the guided sentences (or upload recordings), correct what the
	// transcriber heard, then the live consent check, then a preview. Nothing plays by itself.
	let { voice, engineReady, onchange }: { voice: Voice; engineReady: boolean; onchange: () => void } = $props();

	let script = $state<string[]>([]);
	let line = $state(0);
	let recorder = new VoiceRecorder();
	let recording = $state<'take' | 'consent' | null>(null);
	let working = $state('');
	let challenge = $state<ConsentChallenge | null>(null);
	let consentResult = $state<ConsentResult | null>(null);
	let editing = $state<number | null>(null);
	let draft = $state('');
	let previewText = $state('This is my narration voice. It reads summaries, digests and notes aloud for me.');
	let previewId = $state<string | null>(null);
	let fileInput = $state<HTMLInputElement | null>(null);

	onMount(async () => {
		script = await getVoiceScript().catch(() => []);
		// Start where the takes left off.
		const said = new Set(voice.clips.filter((c) => c.accepted).map((c) => c.text));
		line = Math.max(0, script.findIndex((s) => !said.has(s)));
	});
	onDestroy(() => recorder.cancel());

	async function run<T>(label: string, work: () => Promise<T>): Promise<T | null> {
		working = label;
		try {
			return await work();
		} catch (e) {
			toastState.error(plainError(e, `${label} failed`));
			return null;
		} finally {
			working = '';
		}
	}

	async function startTake(kind: 'take' | 'consent') {
		try {
			await recorder.start();
			recording = kind;
		} catch (e) {
			toastState.error(plainError(e, 'Could not open the microphone'));
		}
	}

	async function stopTake() {
		const kind = recording;
		recording = null;
		const take = await recorder.stop();
		if (kind === 'take') {
			const text = script[line] ?? '';
			const job = await run('Checking the recording', async () => waitForJob(await addVoiceAudio(voice.id, take.blob, take.filename, text, true)));
			if (job?.status === 'failed') toastState.error(job.error || 'The recording was not added');
			const newest = (job?.result as { voice?: Voice } | null)?.voice?.clips.at(-1);
			if (newest?.accepted) line = Math.min(line + 1, script.length - 1);
			else if (newest) toastState.error(newest.problem || 'That take could not be used');
			onchange();
		} else if (kind === 'consent') {
			const job = await run('Checking the statement', async () => waitForJob(await sendConsent(voice.id, take.blob, take.filename)));
			if (job?.status === 'failed') toastState.error(job.error || 'The consent check could not run');
			consentResult = (job?.result as ConsentResult | null) ?? null;
			challenge = null;
			onchange();
		}
	}

	async function upload(e: Event) {
		const file = (e.currentTarget as HTMLInputElement).files?.[0];
		(e.currentTarget as HTMLInputElement).value = '';
		if (!file) return;
		const job = await run('Checking the upload', async () => waitForJob(await addVoiceAudio(voice.id, file, file.name)));
		if (job?.status === 'failed') toastState.error(job.error || 'The upload was not added');
		onchange();
	}

	async function saveText(idx: number) {
		await run('Saving', () => correctVoiceClip(voice.id, idx, draft));
		editing = null;
		onchange();
	}

	async function removeClip(idx: number) {
		await run('Removing', () => deleteVoiceClip(voice.id, idx));
		onchange();
	}

	// The clip narration copies. Picking it by ear beat every automatic choice we measured.
	async function useAsReference(idx: number) {
		await run('Saving', () => setVoiceReference(voice.id, voice.reference_idx === idx ? -1 : idx));
		onchange();
	}

	async function askConsent() {
		consentResult = null;
		challenge = await run('Preparing the statement', () => getConsentChallenge(voice.id));
	}

	async function preview() {
		previewId = null;
		const job = await run('Narrating', async () =>
			waitForJob(await narrate({ voice_id: voice.id, source_kind: 'text', text: previewText, title: `${voice.name}: preview`, check: false }))
		);
		if (job?.status === 'failed') toastState.error(job.error || 'The preview failed');
		previewId = (job?.result as { narration_id?: string } | null)?.narration_id ?? null;
	}

	const btn = 'px-3 py-1 text-xs rounded border disabled:opacity-50';
</script>

<div class="space-y-4 text-sm">
	{#if working}
		<p class="text-xs text-gray-300"><span class="inline-block w-2 h-2 rounded-full bg-blue-500 animate-pulse mr-1"></span>{working}…</p>
	{/if}

	<section class="space-y-2" aria-label="Record">
		<h5 class="text-xs font-semibold text-gray-300 uppercase tracking-wide">1 · Record your voice</h5>
		<p class="text-xs text-gray-500">
			Read each sentence in your normal speaking voice, somewhere quiet, close to the microphone. About 20 seconds of good
			recordings is enough; more helps. Or upload recordings of you alone (a few minutes at most).
		</p>
		{#if script.length}
			<blockquote class="border-l-2 border-blue-700 pl-3 text-gray-200">{script[line]}</blockquote>
			<div class="flex flex-wrap items-center gap-2">
				{#if recording === 'take'}
					<button onclick={stopTake} class="{btn} bg-red-700 hover:bg-red-600 border-red-600 text-white">Stop</button>
					<span class="text-xs text-red-300">Recording…</span>
				{:else}
					<button onclick={() => startTake('take')} disabled={!!recording || !!working} class="{btn} bg-blue-700 hover:bg-blue-600 border-blue-600 text-white">Record this sentence</button>
				{/if}
				<button onclick={() => (line = (line + 1) % script.length)} disabled={!!recording} class="{btn} bg-gray-800 hover:bg-gray-700 border-gray-700 text-gray-300">Skip</button>
				<span class="text-xs text-gray-600">{line + 1} of {script.length}</span>
				<button onclick={() => fileInput?.click()} disabled={!!recording || !!working} class="{btn} ml-auto bg-gray-800 hover:bg-gray-700 border-gray-700 text-gray-300">Upload a recording…</button>
				<input bind:this={fileInput} type="file" accept="audio/*,video/*" class="hidden" onchange={upload} />
			</div>
		{/if}
		{#if voice.clips.length}
			<ul class="space-y-1">
				{#each voice.clips as c (c.idx)}
					<li class="flex items-start gap-2 text-xs rounded bg-gray-900 border border-gray-800 px-2 py-1.5">
						<span class="mt-0.5 {c.accepted ? 'text-green-400' : 'text-amber-400'}" title={c.accepted ? 'Used' : 'Not used'}>{c.accepted ? '✓' : '!'}</span>
						<div class="flex-1 space-y-1">
							{#if editing === c.idx}
								<textarea bind:value={draft} rows="2" class="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-gray-200"></textarea>
								<div class="flex gap-2">
									<button onclick={() => saveText(c.idx)} class="text-blue-400 hover:text-blue-300">Save</button>
									<button onclick={() => (editing = null)} class="text-gray-500 hover:text-gray-300">Cancel</button>
								</div>
							{:else}
								<p class="text-gray-300">{c.text || '(no words heard)'}</p>
								{#if c.problem}<p class="text-amber-300">{c.problem}</p>{/if}
							{/if}
							<audio controls preload="none" src={voiceClipUrl(voice.id, c.idx)} class="h-7 w-full max-w-xs"></audio>
						</div>
						<span class="text-gray-600">{c.seconds.toFixed(1)} s</span>
						{#if c.accepted}
							<button
								onclick={() => useAsReference(c.idx)}
								class={voice.reference_idx === c.idx ? 'text-blue-300 hover:text-blue-200' : 'text-gray-500 hover:text-gray-300'}
								title={voice.reference_idx === c.idx ? 'Narration copies this recording; click to let Mnemosyne choose again' : 'Narrate by copying this recording'}
							>{voice.reference_idx === c.idx ? '★ Reference' : 'Sounds like me'}</button>
						{/if}
						{#if editing !== c.idx}
							<button onclick={() => ((editing = c.idx), (draft = c.text))} class="text-gray-500 hover:text-gray-300" title="Correct what is said in it">Edit</button>
						{/if}
						<button onclick={() => removeClip(c.idx)} class="text-gray-500 hover:text-red-400" title="Remove this recording">✕</button>
					</li>
				{/each}
			</ul>
			<p class="text-xs text-gray-500">
				{Math.round(voice.seconds)} s of usable recording. Narration copies one recording of 8–20 s: play a few and mark the
				one that sounds most like you talking naturally with <em>Sounds like me</em>{voice.reference_idx === null || voice.reference_idx === undefined ? ' (until then Mnemosyne picks the clearest)' : ''}.
			</p>
		{/if}
	</section>

	<section class="space-y-2" aria-label="Consent">
		<h5 class="text-xs font-semibold text-gray-300 uppercase tracking-wide">2 · Confirm it is you {voice.consented ? '· done' : ''}</h5>
		<p class="text-xs text-gray-500">
			Read a short statement aloud, now. It includes a few random words shown only this once, and it must be in the same
			voice as your recordings. The recording is kept with the voice as the record of your consent.
		</p>
		{#if challenge}
			<blockquote class="border-l-2 border-amber-600 pl-3 text-gray-100">{challenge.text}</blockquote>
			{#if recording === 'consent'}
				<button onclick={stopTake} class="{btn} bg-red-700 hover:bg-red-600 border-red-600 text-white">Stop</button>
				<span class="text-xs text-red-300">Recording…</span>
			{:else}
				<button onclick={() => startTake('consent')} disabled={!!working} class="{btn} bg-blue-700 hover:bg-blue-600 border-blue-600 text-white">Record the statement</button>
			{/if}
		{:else}
			<button onclick={askConsent} disabled={!!working || voice.seconds === 0} class="{btn} bg-gray-800 hover:bg-gray-700 border-gray-700 text-gray-200">
				{voice.consented ? 'Confirm again' : 'Show the statement'}
			</button>
		{/if}
		{#if consentResult}
			<p class="text-xs {consentResult.passed ? 'text-green-400' : 'text-amber-300'}">
				{consentResult.passed ? 'Confirmed: the voice is yours.' : consentResult.problem}
			</p>
		{/if}
	</section>

	<section class="space-y-2" aria-label="Preview">
		<h5 class="text-xs font-semibold text-gray-300 uppercase tracking-wide">3 · Hear it</h5>
		{#if !voice.ready}
			<p class="text-xs text-gray-500">Ready once it has {voice.needs}.</p>
		{:else if !engineReady}
			<p class="text-xs text-gray-500">Install the narration engine above to hear it.</p>
		{:else}
			<textarea bind:value={previewText} rows="2" class="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200"></textarea>
			<button onclick={preview} disabled={!!working || !previewText.trim()} class="{btn} bg-blue-700 hover:bg-blue-600 border-blue-600 text-white">Narrate this</button>
			{#if previewId}
				<audio controls src={narrationAudioUrl(previewId)} class="w-full max-w-md"></audio>
			{/if}
		{/if}
	</section>
</div>
