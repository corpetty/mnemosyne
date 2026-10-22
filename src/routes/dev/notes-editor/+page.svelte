<script lang="ts">
	// Development-only harness for the notes editor: a local document with the DOM and input
	// layers, no backend (tests-e2e/notes-editor.spec.ts and the WebKitGTK probe drive it
	// through `window.__notes`). Everything editor-related is imported under
	// `import.meta.env.DEV`, so production builds carry only this stub.
	import { onMount } from 'svelte';

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

	let root: HTMLElement;
	let status = $state('loading');

	onMount(async () => {
		if (!import.meta.env.DEV) {
			status = 'development only';
			return;
		}
		const params = new URLSearchParams(location.search);
		const md = params.has('md') ? (params.get('md') ?? '') : SAMPLE;
		const [{ mountHarness }] = await Promise.all([
			import('$lib/notes/editor/harness.js'),
			import('$lib/notes/editor/editor.css')
		]);
		const harness = await mountHarness(root, md);
		(window as unknown as { __notes: unknown }).__notes = harness;
		if (params.has('readonly')) harness.input.setReadOnly(true);
		status = 'ready';
	});
</script>

<svelte:head>
	<title>Notes editor harness</title>
</svelte:head>

<div class="mx-auto max-w-3xl p-6">
	<p class="mb-3 text-xs text-gray-400" data-testid="status">{status}</p>
	<div bind:this={root} class="min-h-64 rounded border border-gray-700 p-3" data-testid="editor"></div>
</div>
