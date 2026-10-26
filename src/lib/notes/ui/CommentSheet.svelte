<script lang="ts">
	// Writing a comment on touch screens (the touch bar's Comment): a Sheet above the keyboard with
	// the quoted selection (two lines at most), a text box that takes focus, Cancel and Comment.
	// `onsubmit` posts through the host (NotesHost.postComment); while it runs the buttons wait, and
	// when it throws the sheet stays open with the text kept (the editor tells the person why).
	import { onMount } from 'svelte';
	import Sheet from './Sheet.svelte';

	let {
		quote,
		onsubmit,
		oncancel
	}: {
		/** The selected text the comment is on. */
		quote: string;
		onsubmit: (text: string) => Promise<void> | void;
		oncancel: () => void;
	} = $props();

	let text = $state('');
	let busy = $state(false);
	let box = $state<HTMLTextAreaElement>();
	const empty = $derived(text.trim() === '');

	onMount(() => box?.focus({ preventScroll: true }));

	async function send() {
		if (empty || busy) return;
		busy = true;
		try {
			await onsubmit(text.trim());
		} catch {
			// Kept open with the text: the editor has said what went wrong.
		} finally {
			busy = false;
		}
	}

	function keydown(e: KeyboardEvent) {
		if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !e.isComposing) {
			e.preventDefault();
			void send();
		}
	}
</script>

<Sheet label="Comment on the selection" testid="comment-sheet" onclose={oncancel}>
	<form
		class="space-y-2"
		onsubmit={(e) => {
			e.preventDefault();
			void send();
		}}
	>
		<p class="text-[11px] text-gray-500">Commenting on</p>
		<blockquote
			data-testid="comment-sheet-quote"
			class="border-l-2 border-yellow-500/60 pl-2 text-xs text-gray-300 break-words line-clamp-2"
		>
			{quote}
		</blockquote>
		<textarea
			bind:this={box}
			bind:value={text}
			onkeydown={keydown}
			rows="2"
			placeholder="Comment on this text"
			aria-label="Your comment"
			class="w-full resize-none bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-base text-gray-200 placeholder:text-gray-600"
		></textarea>
		<div class="flex items-center justify-end gap-2">
			<button
				type="button"
				onclick={oncancel}
				class="min-h-10 px-3 text-sm rounded border border-gray-700 bg-gray-900 text-gray-300">Cancel</button
			>
			<button
				type="submit"
				disabled={empty || busy}
				class="min-h-10 px-3 text-sm rounded bg-blue-600 text-white disabled:opacity-40">Comment</button
			>
		</div>
	</form>
</Sheet>
