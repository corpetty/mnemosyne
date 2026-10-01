<script lang="ts">
	import { addExternalNotes, deleteExternalNotes, uploadExternalNotes } from '$lib/api/backend.js';
	import { canChange } from '$lib/app/access.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';

	// Notes another assistant wrote (Gemini in Google Meet, Zoom AI Companion, Otter, Teams
	// Copilot...). The summary fills gaps from them; with no recording, it is made from them.
	const SOURCES = ['', 'Gemini', 'Zoom', 'Otter', 'Teams Copilot', 'Fireflies', 'Fathom', 'Other'];
	const session = $derived(sessionState.activeSession);
	const notes = $derived(session?.external_notes ?? []);
	let adding = $state(false);
	let text = $state('');
	let source = $state('');
	let busy = $state(false);

	async function run(op: () => Promise<unknown>, ok: string) {
		busy = true;
		try {
			await op();
			await sessionState.refreshActive();
			toastState.success(ok);
			adding = false;
			text = '';
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not add the notes');
		} finally {
			busy = false;
		}
	}

	function chooseFile() {
		if (!session) return;
		const input = document.createElement('input');
		input.type = 'file';
		input.accept = '.md,.txt,.html,.htm,.docx,.pdf,text/*';
		input.onchange = () => {
			const f = input.files?.[0];
			if (f) run(() => uploadExternalNotes(session.id, f, source), `Added ${f.name}`);
		};
		input.click();
	}

	const hint = $derived(
		session?.summary ? 'Re-summarize to fold them into the summary.' : 'Summarize to make a summary from them.'
	);
</script>

{#if session}
	<section class="rounded-lg border border-gray-800 bg-gray-900/40 p-3 space-y-2" aria-label="Notes from other assistants">
		<header class="flex items-baseline gap-2">
			<h4 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500">Notes from other assistants</h4>
			{#if !adding && canChange(session)}
				<button onclick={() => (adding = true)} class="ml-auto text-xs text-blue-400 hover:text-blue-300">Add notes…</button>
			{/if}
		</header>
		{#each notes as n (n.id)}
			<details class="rounded border border-gray-800 bg-gray-950/40 px-2 py-1.5 text-sm">
				<summary class="flex cursor-pointer items-center gap-2 text-gray-300">
					<span class="rounded bg-gray-800 px-1.5 text-xs text-gray-300">{n.source}</span>
					<span class="truncate text-xs text-gray-500">{n.filename ?? n.text.slice(0, 80)}</span>
					{#if canChange(session)}<button
						onclick={(e) => {
							e.preventDefault();
							run(() => deleteExternalNotes(session.id, n.id), 'Removed');
						}}
						class="ml-auto px-1 text-gray-500 hover:text-gray-200"
						aria-label={`Remove the ${n.source} notes`}
					>✕</button>{/if}
				</summary>
				<pre class="mt-2 max-h-72 overflow-y-auto whitespace-pre-wrap font-sans text-xs text-gray-400">{n.text}</pre>
			</details>
		{/each}
		{#if notes.length && !adding}<p class="text-xs text-gray-500">{hint}</p>{/if}
		{#if !notes.length && !adding}
			<p class="text-xs text-gray-500">
				Paste "Notes by Gemini", Zoom's meeting summary, Otter or Teams Copilot notes. The summary fills gaps from them (the
				transcript wins); a meeting you didn't record can be summarized from them alone.
			</p>
		{/if}
		{#if adding}
			<div class="space-y-2">
				<label class="flex items-center gap-2 text-xs text-gray-400">
					From
					<select bind:value={source} class="bg-gray-800 border border-gray-700 rounded px-1.5 py-1 text-sm text-gray-200" aria-label="From">
						{#each SOURCES as s (s)}<option value={s}>{s || 'Recognize it'}</option>{/each}
					</select>
				</label>
				<textarea
					bind:value={text}
					rows="6"
					placeholder="Paste the notes here"
					class="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200"
				></textarea>
				<div class="flex gap-2">
					<button
						onclick={() => session && run(() => addExternalNotes(session.id, text, source), 'Notes added')}
						disabled={busy || !text.trim()}
						class="px-3 py-1 text-sm rounded bg-blue-700 hover:bg-blue-600 text-white disabled:opacity-50"
					>Add</button>
					<button onclick={chooseFile} disabled={busy} class="px-3 py-1 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300">Upload a file…</button>
					<button onclick={() => (adding = false)} class="px-2 text-sm text-gray-400 hover:text-gray-200">Cancel</button>
				</div>
			</div>
		{/if}
	</section>
{/if}
