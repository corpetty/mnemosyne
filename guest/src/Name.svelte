<script lang="ts">
	// "Your name" before joining (first visit), and again from "Change name" in the header. The
	// consent line says what typing here means; the name is kept in the page's localStorage by
	// the caller (identity.ts).
	import { NAME_MAX, cleanName } from './identity';

	let {
		owner = '',
		initial = '',
		onjoin,
		oncancel
	}: {
		/** The meeting owner's name when known (meta), else "". */
		owner?: string;
		initial?: string;
		onjoin: (name: string) => void;
		/** Given when changing the name: a Cancel button closes the card. */
		oncancel?: () => void;
	} = $props();

	// The starting value only; the field is the user's from then on.
	// svelte-ignore state_referenced_locally
	let name = $state(initial);
	const clean = $derived(cleanName(name));

	function submit(e: SubmitEvent) {
		e.preventDefault();
		if (clean) onjoin(clean);
	}
</script>

<svelte:window
	onkeydown={(e) => {
		if (e.key === 'Escape' && oncancel) {
			e.preventDefault();
			oncancel();
		}
	}}
/>

<form
	class="mx-auto w-full max-w-sm space-y-4 rounded-lg border border-gray-800 bg-gray-900 p-5 shadow-xl"
	aria-labelledby="guest-name-heading"
	onsubmit={submit}
>
	<h1 id="guest-name-heading" class="text-lg font-semibold text-gray-100">
		{owner ? `Join ${owner}'s notes` : 'Join the meeting notes'}
	</h1>
	<label class="block text-sm text-gray-400">
		Your name
		<!-- svelte-ignore a11y_autofocus -->
		<input
			type="text"
			bind:value={name}
			maxlength={NAME_MAX}
			autocomplete="name"
			autofocus
			class="mt-1 block w-full rounded border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-gray-500 focus:outline-none"
		/>
	</label>
	<p class="text-xs text-gray-500">
		What you type here becomes part of {owner ? `${owner}'s` : "the meeting owner's"} notes of this meeting.
	</p>
	<div class="flex gap-2">
		{#if oncancel}
			<button
				type="button"
				onclick={oncancel}
				class="flex-1 rounded border border-gray-700 px-3 py-2 text-sm text-gray-300 hover:text-gray-100">Cancel</button
			>
		{/if}
		<button
			type="submit"
			disabled={!clean}
			class="flex-1 rounded bg-blue-600 px-3 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50 disabled:hover:bg-blue-600"
			>Join</button
		>
	</div>
</form>
