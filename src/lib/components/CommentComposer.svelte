<script lang="ts">
	import { onMount } from 'svelte';
	import { commentsState } from '$lib/stores/comments.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { CommentCreate } from '$lib/types/index.js';

	let {
		sessionId,
		target,
		placeholder = 'Add a comment',
		onposted,
		oncancel,
		onsubmit,
		autofocus = false
	}: {
		sessionId: string;
		target: Omit<CommentCreate, 'text'>;
		placeholder?: string;
		onposted?: () => void;
		oncancel?: () => void;
		/** Posts somewhere other than the open meeting (a task's comments); replaces commentsState.add. */
		onsubmit?: (text: string) => Promise<void>;
		autofocus?: boolean;
	} = $props();

	let text = $state('');
	let busy = $state(false);
	let box = $state<HTMLTextAreaElement | null>(null);

	const empty = $derived(text.trim() === '');

	onMount(() => {
		if (autofocus) box?.focus();
	});

	function grow() {
		if (!box) return;
		box.style.height = 'auto';
		box.style.height = `${Math.min(box.scrollHeight, 240)}px`;
	}

	async function post() {
		if (empty || busy) return;
		busy = true;
		try {
			if (onsubmit) await onsubmit(text.trim());
			else await commentsState.add({ ...target, text: text.trim() } as CommentCreate);
			text = '';
			grow();
			onposted?.();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not post the comment');
		} finally {
			busy = false;
		}
	}

	function keydown(e: KeyboardEvent) {
		if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
			e.preventDefault();
			post();
		} else if (e.key === 'Escape') {
			e.preventDefault();
			oncancel?.();
		}
	}
</script>

<div class="space-y-1.5" data-session={sessionId}>
	<textarea
		bind:this={box}
		bind:value={text}
		oninput={grow}
		onkeydown={keydown}
		rows="2"
		{placeholder}
		class="w-full resize-none bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 placeholder:text-gray-600"
	></textarea>
	<div class="flex items-center gap-2">
		<button
			onclick={post}
			disabled={empty || busy}
			class="px-2.5 py-1 text-xs rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50"
		>Comment</button>
		<button onclick={() => oncancel?.()} class="px-2 py-1 text-xs text-gray-400 hover:text-gray-200">Cancel</button>
	</div>
</div>
