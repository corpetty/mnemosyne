<script lang="ts">
	// The name a reader comments under, asked for inline before their first comment (the guest
	// page's Name.svelte, in the comments' sizes). The caller keeps it (comments.ts ReaderIdentity).
	import { NAME_MAX, cleanName } from './comments';

	let { onname }: { onname: (name: string) => void } = $props();

	let name = $state('');
	const clean = $derived(cleanName(name));

	function submit(e: SubmitEvent) {
		e.preventDefault();
		if (clean) onname(clean);
	}
</script>

<form class="space-y-2" onsubmit={submit} data-testid="reader-name">
	<label class="block text-xs text-gray-400">
		Your name, shown with your comments
		<input
			type="text"
			bind:value={name}
			maxlength={NAME_MAX}
			autocomplete="name"
			class="mt-1 block w-full rounded border border-gray-700 bg-gray-800 px-2 py-1.5 text-sm text-gray-200 focus:border-gray-500 focus:outline-none"
		/>
	</label>
	<p class="text-xs text-gray-500">This browser remembers you. In another browser, or after clearing this one, you comment as someone new.</p>
	<button
		type="submit"
		disabled={!clean}
		class="rounded bg-blue-600 px-2.5 py-1 text-xs text-white hover:bg-blue-500 disabled:opacity-50">Continue</button
	>
</form>
