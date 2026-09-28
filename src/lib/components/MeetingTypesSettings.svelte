<script lang="ts">
	import { getSettings, listSummaryStyles, updateSettings } from '$lib/api/backend.js';
	import { loadAutoRecordSettings } from '$lib/app/controller.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { SummaryStyle } from '$lib/types/index.js';

	// Kinds of meeting (backend services/meeting_types.py): a meeting whose title has one of
	// a type's words gets its summary style and instructions, Obsidian folder and local-only.
	type Kind = {
		name: string;
		match: string;
		summary_style: string;
		instructions: string;
		obsidian_folder: string;
		local_only: boolean;
		auto_record: boolean;
	};
	let kinds = $state<Kind[]>([]);
	let saved = $state('');
	let styles = $state<SummaryStyle[]>([]);
	let busy = $state(false);

	$effect(() => {
		Promise.all([getSettings(), listSummaryStyles()])
			.then(([s, st]) => {
				kinds = (s.values.meeting_types ?? []).map((k) => ({ ...k }));
				saved = JSON.stringify(kinds);
				styles = st;
			})
			.catch(() => {});
	});

	const changed = $derived(JSON.stringify(kinds) !== saved);
	const blank = (): Kind => ({ name: '', match: '', summary_style: '', instructions: '', obsidian_folder: '', local_only: false, auto_record: false });

	async function save() {
		const clean = kinds.filter((k) => k.name.trim()).map((k) => ({ ...k, name: k.name.trim() }));
		if (new Set(clean.map((k) => k.name)).size !== clean.length) {
			toastState.error('Two meeting types have the same name');
			return;
		}
		busy = true;
		try {
			const s = await updateSettings({ meeting_types: clean });
			kinds = (s.values.meeting_types ?? []).map((k) => ({ ...k }));
			saved = JSON.stringify(kinds);
			loadAutoRecordSettings();
			toastState.success('Meeting types saved');
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not save');
		} finally {
			busy = false;
		}
	}

	const input = 'w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200';
	const label = 'block text-xs text-gray-500 mb-1';
</script>

<section>
	<h3 class="text-lg font-semibold text-gray-200 mb-1">Meeting types</h3>
	<p class="text-xs text-gray-500 mb-3">
		A meeting whose title has one of a type's words (when it is named from the calendar, by you, or by its summary) takes
		that type's settings. Choose another in the meeting's header.
	</p>
	<div class="space-y-3">
		{#each kinds as kind, i (i)}
			<div class="rounded-lg border border-gray-800 bg-gray-900/40 p-3 grid gap-3 sm:grid-cols-2" aria-label={`Meeting type ${kind.name || i + 1}`}>
				<label><span class={label}>Name</span><input bind:value={kind.name} placeholder="e.g. Standup" class={input} /></label>
				<label><span class={label}>Title contains (comma-separated)</span><input bind:value={kind.match} placeholder="standup, daily sync" class={input} /></label>
				<label>
					<span class={label}>Summary style</span>
					<select bind:value={kind.summary_style} class={input}>
						<option value="">Default</option>
						{#each styles as st (st.id)}<option value={st.id}>{st.id}</option>{/each}
					</select>
				</label>
				<label><span class={label}>Obsidian folder (blank: the default)</span><input bind:value={kind.obsidian_folder} placeholder="meetings/standups" class={input} /></label>
				<label class="sm:col-span-2"><span class={label}>Extra summary instructions</span><textarea bind:value={kind.instructions} rows="2" placeholder="e.g. List blockers per person." class={input}></textarea></label>
				<label class="flex items-center gap-2"><input type="checkbox" bind:checked={kind.local_only} class="rounded border-gray-600 bg-gray-800" /><span class="text-sm text-gray-300">Local only (never a cloud model)</span></label>
				<label class="flex items-center gap-2"><input type="checkbox" bind:checked={kind.auto_record} class="rounded border-gray-600 bg-gray-800" /><span class="text-sm text-gray-300">Record when such a calendar meeting starts</span></label>
				<button onclick={() => (kinds = kinds.filter((_, j) => j !== i))} class="sm:col-span-2 justify-self-start text-xs text-gray-500 hover:text-red-300">Remove this type</button>
			</div>
		{/each}
	</div>
	<div class="mt-3 flex gap-3">
		<button onclick={() => (kinds = [...kinds, blank()])} class="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300">Add a meeting type</button>
		<button onclick={save} disabled={busy || !changed} class="px-3 py-1.5 text-sm rounded bg-blue-700 hover:bg-blue-600 text-white disabled:opacity-50">Save meeting types</button>
	</div>
</section>
