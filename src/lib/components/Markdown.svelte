<script module lang="ts">
	import { marked } from 'marked';
	import DOMPurify from 'dompurify';

	marked.setOptions({ gfm: true, breaks: true });

	// Registered once per app, not per component instance.
	DOMPurify.addHook('afterSanitizeAttributes', (node) => {
		if (node.tagName === 'A') {
			node.setAttribute('target', '_blank');
			node.setAttribute('rel', 'noopener noreferrer');
		}
	});

	export function renderMarkdown(text: string): string {
		return DOMPurify.sanitize(marked.parse(text ?? '', { async: false }) as string);
	}

	/** Open links in the system browser: Tauri's shell plugin, or a new tab in a plain browser. */
	async function openExternal(href: string) {
		try {
			const { invoke } = await import('@tauri-apps/api/core');
			await invoke('plugin:shell|open', { path: href });
		} catch {
			window.open(href, '_blank', 'noopener,noreferrer');
		}
	}
</script>

<script lang="ts">
	let { text = '' }: { text?: string } = $props();
	const html = $derived(renderMarkdown(text));

	function onClick(e: MouseEvent) {
		const a = (e.target as HTMLElement).closest('a');
		const href = a?.getAttribute('href');
		if (!a || !href || href.startsWith('#')) return;
		e.preventDefault();
		openExternal(href);
	}
</script>

<!-- Link clicks are delegated here; the links themselves stay keyboard-focusable. -->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<div class="md" role="presentation" onclick={onClick}>{@html html}</div>

<style>
	.md {
		font-size: 0.875rem;
		line-height: 1.55;
		color: rgb(229 231 235);
		overflow-wrap: anywhere;
	}
	.md :global(> :first-child) {
		margin-top: 0;
	}
	.md :global(> :last-child) {
		margin-bottom: 0;
	}
	.md :global(h1),
	.md :global(h2),
	.md :global(h3),
	.md :global(h4) {
		color: rgb(243 244 246);
		font-weight: 600;
		margin: 0.9em 0 0.4em;
		line-height: 1.3;
	}
	.md :global(h1) {
		font-size: 1.15rem;
	}
	.md :global(h2) {
		font-size: 1.05rem;
	}
	.md :global(h3),
	.md :global(h4) {
		font-size: 0.95rem;
	}
	.md :global(p) {
		margin: 0 0 0.6em;
	}
	.md :global(ul),
	.md :global(ol) {
		margin: 0 0 0.6em;
		padding-left: 1.3em;
	}
	.md :global(ul) {
		list-style: disc;
	}
	.md :global(ol) {
		list-style: decimal;
	}
	.md :global(li) {
		margin: 0.15em 0;
	}
	.md :global(li > ul),
	.md :global(li > ol) {
		margin: 0.15em 0 0;
	}
	.md :global(input[type='checkbox']) {
		margin-right: 0.4em;
	}
	.md :global(a) {
		color: rgb(96 165 250);
		text-decoration: underline;
		text-underline-offset: 2px;
	}
	.md :global(strong) {
		color: rgb(243 244 246);
		font-weight: 600;
	}
	.md :global(code) {
		font-family: ui-monospace, monospace;
		font-size: 0.8em;
		background: rgb(31 41 55);
		border-radius: 0.25rem;
		padding: 0.1em 0.3em;
	}
	.md :global(pre) {
		background: rgb(17 24 39);
		border: 1px solid rgb(55 65 81);
		border-radius: 0.375rem;
		padding: 0.6em 0.8em;
		overflow-x: auto;
		margin: 0 0 0.6em;
	}
	.md :global(pre code) {
		background: none;
		padding: 0;
	}
	.md :global(blockquote) {
		border-left: 3px solid rgb(75 85 99);
		padding-left: 0.8em;
		color: rgb(156 163 175);
		margin: 0 0 0.6em;
	}
	.md :global(hr) {
		border-color: rgb(55 65 81);
		margin: 0.9em 0;
	}
	.md :global(table) {
		border-collapse: collapse;
		margin: 0 0 0.6em;
		display: block;
		overflow-x: auto;
	}
	.md :global(th),
	.md :global(td) {
		border: 1px solid rgb(55 65 81);
		padding: 0.25em 0.6em;
		text-align: left;
	}
	.md :global(th) {
		background: rgb(31 41 55);
	}
</style>
