<script lang="ts">
	// Development-only harness for the notes editor: a local document with the DOM and input
	// layers, no backend (tests-e2e/notes-editor.spec.ts and the WebKitGTK probe drive it
	// through `window.__notes`). Everything editor-related is imported under
	// `import.meta.env.DEV`, so production builds carry only this stub.
	import { onMount } from 'svelte';
	import type { NotesHost } from '$lib/notes/ui/types.js';
	import type NotesEditorType from '$lib/notes/ui/NotesEditor.svelte';

	const SAMPLE = [
		'# Notes',
		'',
		'Plain text with **bold** and `code`.',
		'',
		'- first point',
		'- second point',
		'',
		'| a | b |',
		'|---|---|',
		'| 1 | 2 |',
		''
	].join('\n');

	let status = $state('loading');
	let readOnly = $state(false);
	let ready = $state(false);
	let host = $state.raw<NotesHost | null>(null);
	let Editor = $state.raw<typeof NotesEditorType | null>(null);
	let harness: unknown = null;

	// `window.__notes` appears once the editor has mounted on the document.
	$effect(() => {
		if (!ready || !harness) return;
		(window as unknown as { __notes: unknown }).__notes = harness;
		status = 'ready';
	});

	onMount(async () => {
		if (!import.meta.env.DEV) {
			status = 'development only';
			return;
		}
		const params = new URLSearchParams(location.search);
		const md = params.has('md') ? (params.get('md') ?? '') : SAMPLE;
		readOnly = params.has('readonly');
		const [{ mountHarness }, ed] = await Promise.all([
			import('$lib/notes/editor/harness.js'),
			import('$lib/notes/ui/NotesEditor.svelte')
		]);
		const h = await mountHarness(md, { get: () => readOnly, set: (v) => (readOnly = v) });
		harness = h;
		Editor = ed.default;
		host = h.host;
	});
</script>

<svelte:head>
	<title>Notes editor harness</title>
</svelte:head>

<div class="mx-auto max-w-3xl p-6">
	<p class="mb-3 text-xs text-gray-400" data-testid="status">{status}</p>
	{#if Editor && host}
		<Editor {host} testid="editor" label="Notes" bind:ready />
	{/if}
</div>
