<script lang="ts">
	import { sessionState } from '$lib/stores/session.svelte.js';
	import Markdown from './Markdown.svelte';

	let saveTimer: ReturnType<typeof setTimeout> | null = null;
	let pending: { sessionId: string; notes: string } | null = null;
	let localNotes = $state('');
	let lastSavedSessionId = $state<string | null>(null);
	let mode = $state<'edit' | 'preview'>('edit');
	let saveState = $state<'saved' | 'typing' | 'saving' | 'failed'>('saved');
	let failed: { sessionId: string; notes: string } | null = null;

	async function flush() {
		if (saveTimer) clearTimeout(saveTimer);
		saveTimer = null;
		if (!pending) return;
		const edit = pending;
		pending = null;
		const current = () => edit.sessionId === sessionState.activeSession?.id;
		if (current()) saveState = 'saving';
		const ok = await sessionState.updateNotes(edit.sessionId, edit.notes);
		failed = ok ? null : edit;
		// A newer edit may be waiting (typed while this one was saving); leave its state alone.
		if (current() && !pending) saveState = ok ? 'saved' : 'failed';
	}

	function retry() {
		if (failed && !pending) pending = failed;
		flush();
	}

	// Sync local notes when the active session changes, saving any pending edit
	// to the session it was typed into first.
	$effect(() => {
		const session = sessionState.activeSession;
		if (session && session.id !== lastSavedSessionId) {
			flush();
			localNotes = session.notes;
			lastSavedSessionId = session.id;
			saveState = 'saved';
			mode = session.notes ? 'preview' : 'edit';
		}
	});

	$effect(() => () => flush());

	function handleInput(e: Event) {
		const session = sessionState.activeSession;
		if (!session) return;
		localNotes = (e.target as HTMLTextAreaElement).value;
		pending = { sessionId: session.id, notes: localNotes };
		saveState = 'typing';
		// Auto-save after 1 second of inactivity
		if (saveTimer) clearTimeout(saveTimer);
		saveTimer = setTimeout(flush, 1000);
	}
</script>

<div class="space-y-2">
	<div class="flex items-center justify-between">
		<div class="flex rounded border border-gray-700 overflow-hidden text-xs">
			<button
				onclick={() => (mode = 'edit')}
				class="px-3 py-1 {mode === 'edit' ? 'bg-gray-700 text-gray-100' : 'bg-gray-900 text-gray-400 hover:text-gray-200'}"
			>
				Edit
			</button>
			<button
				onclick={() => { flush(); mode = 'preview'; }}
				class="px-3 py-1 {mode === 'preview' ? 'bg-gray-700 text-gray-100' : 'bg-gray-900 text-gray-400 hover:text-gray-200'}"
			>
				Preview
			</button>
		</div>
		<span class="text-[11px] text-gray-600" aria-live="polite">
			{#if saveState === 'failed'}
				<span class="text-red-400">Not saved</span>
				<button onclick={retry} class="ml-1 underline text-red-300 hover:text-red-200">Try again</button>
			{:else if saveState === 'saving'}
				Saving…
			{:else if saveState === 'typing'}
				Markdown supported · saves as you pause
			{:else}
				Markdown supported · saved
			{/if}
		</span>
	</div>

	{#if mode === 'edit'}
		<textarea
			value={localNotes}
			oninput={handleInput}
			onblur={flush}
			placeholder="Add notes about this session... (markdown: # headings, - lists, **bold**, [links](https://...))"
			class="w-full h-64 px-3 py-2 text-sm font-mono bg-gray-900 border border-gray-700 rounded-lg text-gray-200 placeholder-gray-600 resize-y focus:outline-none focus:border-gray-500"
		></textarea>
	{:else}
		<div
			role="presentation"
			class="min-h-32 px-4 py-3 bg-gray-900 border border-gray-700 rounded-lg"
			ondblclick={() => (mode = 'edit')}
			title="Double-click to edit"
		>
			{#if localNotes.trim()}
				<Markdown text={localNotes} />
			{:else}
				<p class="text-sm text-gray-600">No notes yet. Switch to Edit to add some.</p>
			{/if}
		</div>
	{/if}
</div>
