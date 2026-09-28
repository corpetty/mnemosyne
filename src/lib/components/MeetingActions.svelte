<script lang="ts">
	import { combineSessions, getJob, importAudio } from '$lib/api/backend.js';
	import { audioState } from '$lib/stores/audio.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { transcriptState } from '$lib/stores/transcript.svelte.js';
	import { uiState } from '$lib/stores/ui.svelte.js';

	// More for the open meeting: add an audio file as its next part, or combine another meeting
	// into it (backend services/combine.py; e.g. a call that dropped and was recorded again).
	let open = $state(false);
	let picking = $state(false);
	let filter = $state('');
	let busy = $state(false);

	const session = $derived(sessionState.activeSession);
	const recordingHere = $derived(audioState.isRecording && audioState.activeSessionId === session?.id);

	// The other meetings with audio, closest in time first: the other half of a dropped call is
	// usually minutes away.
	const candidates = $derived.by(() => {
		if (!session) return [];
		const at = new Date(session.created_at).getTime();
		const q = filter.trim().toLowerCase();
		return sessionState.sessions
			.filter((s) => s.id !== session.id && s.has_audio && (!q || s.name.toLowerCase().includes(q)))
			.sort((a, b) => Math.abs(new Date(a.created_at).getTime() - at) - Math.abs(new Date(b.created_at).getTime() - at))
			.slice(0, 30);
	});

	async function waitFor(jobId: string) {
		for (;;) {
			const j = await getJob(jobId);
			if (j.status === 'completed' || j.status === 'failed' || j.status === 'cancelled') return j;
			await new Promise((r) => setTimeout(r, 700));
		}
	}

	/** A file chooser made on demand (the sidebar has the page's only lasting file input). */
	function chooseFile() {
		const input = document.createElement('input');
		input.type = 'file';
		input.accept = 'audio/*,video/*';
		input.onchange = () => {
			const file = input.files?.[0];
			if (file) addFile(file);
		};
		input.click();
	}

	async function addFile(file: File) {
		if (!session) return;
		busy = true;
		try {
			const res = await importAudio(file, { sessionId: session.id });
			sessionState.update(res.session);
			if (res.job_id) {
				transcriptState.expectJob(session.id, 'Transcribing the added part…');
				uiState.activeTab = 'transcript';
			}
			toastState.success(`Added ${file.name} as the next part`);
			await sessionState.loadSessions();
		} catch (err) {
			toastState.error(err instanceof Error ? err.message : 'Could not add the file');
		} finally {
			busy = false;
		}
	}

	async function combine(otherId: string, otherName: string) {
		if (!session) return;
		const ok = confirm(
			`Combine “${otherName}” into “${session.name}”?\n\nTheir parts are put in the order they were recorded, speakers ` +
				`are matched by voice, and “${otherName}” is then removed (its audio, transcript and notes move here).`
		);
		if (!ok) return;
		picking = false;
		busy = true;
		const id = session.id;
		try {
			toastState.info('Combining the meetings…');
			const job = await waitFor((await combineSessions(id, otherId)).id);
			if (job.status !== 'completed') throw new Error(job.error ?? 'Could not combine the meetings');
			await sessionState.loadSessions();
			if (sessionState.activeSession?.id === id) await sessionState.refreshActive();
			toastState.success(`Combined: ${(job.result as { parts?: number } | null)?.parts ?? 2} parts`);
		} catch (err) {
			toastState.error(err instanceof Error ? err.message : 'Could not combine the meetings');
		} finally {
			busy = false;
		}
	}
</script>

<span class="relative">
	<button
		onclick={() => (open = !open)}
		disabled={busy || recordingHere}
		aria-label="More for this meeting"
		aria-expanded={open}
		class="px-2 py-0.5 rounded border border-gray-800 text-gray-400 hover:text-gray-200 disabled:opacity-50"
	>
		{busy ? '…' : '⋯'}
	</button>
	{#if open}
		<div
			class="absolute right-0 z-30 mt-1 w-64 rounded-lg border border-gray-700 bg-gray-900 py-1 text-sm shadow-xl"
			role="menu"
		>
			<button
				role="menuitem"
				class="block w-full px-3 py-1.5 text-left text-gray-200 hover:bg-gray-800"
				onclick={() => {
					open = false;
					chooseFile();
				}}
			>
				Add an audio file…
				<span class="block text-xs text-gray-500">After what this meeting has, as its next part</span>
			</button>
			<button
				role="menuitem"
				class="block w-full px-3 py-1.5 text-left text-gray-200 hover:bg-gray-800"
				onclick={() => {
					open = false;
					filter = '';
					picking = true;
				}}
			>
				Combine with another meeting…
				<span class="block text-xs text-gray-500">E.g. a call that dropped and was recorded again</span>
			</button>
		</div>
	{/if}
</span>

{#if picking}
	<div class="fixed inset-0 z-40 flex items-start justify-center bg-black/50 px-4 pt-[12vh]" role="presentation" onclick={() => (picking = false)}>
		<div
			class="w-full max-w-lg space-y-3 rounded-lg border border-gray-700 bg-gray-900 p-4 shadow-2xl"
			role="dialog"
			aria-modal="true"
			aria-label="Combine with another meeting"
			tabindex="-1"
			onclick={(e) => e.stopPropagation()}
			onkeydown={(e) => e.key === 'Escape' && (picking = false)}
		>
			<h3 class="text-lg font-semibold text-gray-100">Combine with “{session?.name}”</h3>
			<input
				bind:value={filter}
				placeholder="Find a meeting"
				class="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200"
			/>
			<ul class="max-h-80 overflow-y-auto divide-y divide-gray-800" aria-label="Meetings">
				{#each candidates as c (c.id)}
					<li>
						<button onclick={() => combine(c.id, c.name)} class="flex w-full items-baseline gap-3 px-2 py-2 text-left hover:bg-gray-800">
							<span class="truncate text-sm text-gray-200">{c.name}</span>
							<span class="ml-auto shrink-0 text-xs text-gray-500">{new Date(c.created_at).toLocaleString()}</span>
						</button>
					</li>
				{:else}
					<li class="px-2 py-2 text-sm text-gray-500">No other meeting with audio.</li>
				{/each}
			</ul>
		</div>
	</div>
{/if}
