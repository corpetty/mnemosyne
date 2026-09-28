<script lang="ts">
	import { collectDiagnostics, ISSUES_URL, openExternal } from '$lib/app/diagnostics.js';
	import { toastState } from '$lib/stores/toast.svelte.js';

	let { onclose }: { onclose: () => void } = $props();

	let title = $state('');
	let description = $state('');
	let diagnostics = $state('Collecting…');
	let ready = $state(false);

	$effect(() => {
		collectDiagnostics()
			.then((d) => {
				diagnostics = d.text;
				ready = true;
			})
			.catch((e) => (diagnostics = `Could not collect diagnostics: ${e instanceof Error ? e.message : e}`));
	});

	// Nothing goes to GitHub until you submit the issue there. The report travels by clipboard,
	// not in the link: a URL would reach GitHub as soon as the page opens.
	async function report() {
		const body = `${description.trim() || '(what happened, and what you expected)'}\n\n<details><summary>Diagnostics</summary>\n\n\`\`\`\n${diagnostics}\n\`\`\`\n</details>\n`;
		try {
			await navigator.clipboard.writeText(body);
		} catch {
			toastState.error('Could not copy the report; select the text and copy it yourself');
			return;
		}
		const params = new URLSearchParams({ title: title.trim() || 'Problem report', body: 'Paste the report from Mnemosyne here (it is on your clipboard).' });
		await openExternal(`${ISSUES_URL}?${params}`);
		toastState.success('Report copied; paste it into the issue on GitHub');
		onclose();
	}
</script>

<div class="fixed inset-0 z-40 flex items-start justify-center bg-black/50 px-4 pt-[8vh]" role="presentation" onclick={onclose}>
	<div
		class="w-full max-w-2xl space-y-3 rounded-lg border border-gray-700 bg-gray-900 p-4 shadow-2xl"
		role="dialog"
		aria-modal="true"
		aria-label="Report a problem"
		tabindex="-1"
		onclick={(e) => e.stopPropagation()}
		onkeydown={(e) => e.key === 'Escape' && onclose()}
	>
		<h3 class="text-lg font-semibold text-gray-100">Report a problem</h3>
		<p class="text-xs text-gray-500">
			This opens a new issue on GitHub in your browser and copies the report below, for you to paste there. Nothing is
			sent until you submit it. Edit out anything you would rather not post: the log can mention meeting names.
		</p>
		<label class="block">
			<span class="text-xs text-gray-400">Title</span>
			<input bind:value={title} placeholder="e.g. Recording stops after a few minutes" class="mt-1 w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200" />
		</label>
		<label class="block">
			<span class="text-xs text-gray-400">What happened?</span>
			<textarea bind:value={description} rows="3" class="mt-1 w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200"></textarea>
		</label>
		<label class="block">
			<span class="text-xs text-gray-400">Diagnostics (editable)</span>
			<textarea bind:value={diagnostics} rows="10" class="mt-1 w-full bg-gray-950 border border-gray-800 rounded px-2 py-1.5 font-mono text-[11px] text-gray-300"></textarea>
		</label>
		<div class="flex justify-end gap-2">
			<button onclick={onclose} class="px-3 py-1.5 text-sm text-gray-400 hover:text-gray-200">Cancel</button>
			<button onclick={report} disabled={!ready} class="px-3 py-1.5 text-sm rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50">Copy and open GitHub</button>
		</div>
	</div>
</div>
