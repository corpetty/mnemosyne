<script lang="ts">
	import { sessionState } from '$lib/stores/session.svelte.js';
	import type { SessionSummary } from '$lib/types/index.js';
	import SearchBox from './SearchBox.svelte';
	import { importAudio } from '$lib/api/backend.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { transcriptState } from '$lib/stores/transcript.svelte.js';

	const statusBadge: Record<string, string> = {
		created: 'bg-gray-700 text-gray-300',
		recording: 'bg-red-900 text-red-300',
		encoding: 'bg-yellow-900 text-yellow-300',
		transcribing: 'bg-yellow-900 text-yellow-300',
		completed: 'bg-green-900 text-green-300',
		error: 'bg-red-900 text-red-300'
	};

	function formatDate(dateStr: string): string {
		const d = new Date(dateStr);
		return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
	}

	async function handleNew() {
		await sessionState.createSession();
	}

	async function handleSelect(session: SessionSummary) {
		await sessionState.selectSession(session.id);
	}

	async function handleDelete(e: Event, sessionId: string) {
		e.stopPropagation();
		await sessionState.deleteSession(sessionId);
	}

	$effect(() => {
		sessionState.loadSessions();
	});

	let fileInput = $state<HTMLInputElement>();
	let importing = $state(false);
	let dragOver = $state(false);

	async function importFiles(files: FileList | File[] | null) {
		if (!files || files.length === 0) return;
		importing = true;
		let last: string | null = null;
		for (const file of Array.from(files)) {
			try {
				toastState.info(`Importing ${file.name}…`);
				const res = await importAudio(file);
				last = res.session.id;
				if (res.job_id) transcriptState.expectJob(res.session.id);
			} catch (e) {
				toastState.error(`${file.name}: ${e instanceof Error ? e.message : 'import failed'}`);
			}
		}
		importing = false;
		await sessionState.loadSessions();
		if (last) await sessionState.selectSession(last);
	}

	function onDrop(e: DragEvent) {
		e.preventDefault();
		dragOver = false;
		importFiles(e.dataTransfer?.files ?? null);
	}
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
	class="space-y-2 rounded-lg transition-colors {dragOver ? 'ring-2 ring-blue-500/60 bg-blue-950/20' : ''}"
	ondragover={(e) => { e.preventDefault(); dragOver = true; }}
	ondragleave={() => (dragOver = false)}
	ondrop={onDrop}
>
	<SearchBox />
	<div class="flex gap-2">
		<button
			onclick={handleNew}
			class="flex-1 px-3 py-2 text-sm rounded-lg bg-blue-600 hover:bg-blue-700 text-white font-medium transition-colors"
		>
			New Session
		</button>
		<button
			onclick={() => fileInput?.click()}
			disabled={importing}
			title="Import an audio or video file (or drop it here)"
			class="px-3 py-2 text-sm rounded-lg bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 transition-colors disabled:opacity-50"
		>
			{importing ? '…' : 'Import'}
		</button>
		<input
			bind:this={fileInput}
			type="file"
			accept="audio/*,video/*,.ogg,.opus,.wav,.mp3,.m4a,.flac,.webm,.mp4,.mkv"
			multiple
			onchange={(e) => { importFiles((e.currentTarget as HTMLInputElement).files); (e.currentTarget as HTMLInputElement).value = ''; }}
			class="hidden"
		/>
	</div>

	{#if sessionState.loading}
		<p class="text-gray-500 text-sm px-2">Loading...</p>
	{/if}

	<div class="space-y-1">
		{#each sessionState.sessions as session}
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div
				onclick={() => handleSelect(session)}
				onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') handleSelect(session); }}
				role="button"
				tabindex="0"
				class="w-full text-left px-3 py-2 rounded-lg transition-colors cursor-pointer
					{sessionState.activeSession?.id === session.id
					? 'bg-gray-800 border border-gray-600'
					: 'hover:bg-gray-800/50'}"
			>
				<div class="flex items-center justify-between">
					<span class="text-sm font-medium text-gray-200 truncate">{session.name}</span>
					<button
						onclick={(e) => handleDelete(e, session.id)}
						class="text-gray-600 hover:text-red-400 text-xs px-1 transition-colors"
						title="Delete session"
					>
						x
					</button>
				</div>
				<div class="flex items-center gap-2 mt-1">
					<span class="text-xs px-1.5 py-0.5 rounded {statusBadge[session.status] ?? statusBadge.created}">
						{session.status}
					</span>
					<span class="text-xs text-gray-500">{formatDate(session.created_at)}</span>
				</div>
			</div>
		{/each}

		{#if sessionState.sessions.length === 0 && !sessionState.loading}
			<p class="text-gray-500 text-sm text-center py-4">No sessions yet</p>
		{/if}
	</div>
</div>
