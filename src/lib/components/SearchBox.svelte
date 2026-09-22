<script lang="ts">
	import { search } from '$lib/api/backend.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import type { SearchHit } from '$lib/types/index.js';

	let query = $state('');
	let hits = $state<SearchHit[]>([]);
	let searching = $state(false);
	let timer: ReturnType<typeof setTimeout> | null = null;

	function onInput() {
		if (timer) clearTimeout(timer);
		const q = query.trim();
		if (!q) {
			hits = [];
			return;
		}
		timer = setTimeout(async () => {
			searching = true;
			try {
				hits = await search(q);
			} catch {
				hits = [];
			} finally {
				searching = false;
			}
		}, 200);
	}

	/** Split an FTS snippet into plain and highlighted runs (no HTML injection). */
	function runs(snippet: string): { text: string; hit: boolean }[] {
		const out: { text: string; hit: boolean }[] = [];
		const re = /\[\[(.*?)\]\]/g;
		let last = 0;
		let m: RegExpExecArray | null;
		while ((m = re.exec(snippet))) {
			if (m.index > last) out.push({ text: snippet.slice(last, m.index), hit: false });
			out.push({ text: m[1], hit: true });
			last = m.index + m[0].length;
		}
		if (last < snippet.length) out.push({ text: snippet.slice(last), hit: false });
		return out;
	}

	function formatTime(seconds: number): string {
		const m = Math.floor(seconds / 60);
		const s = Math.floor(seconds % 60);
		return `${m}:${String(s).padStart(2, '0')}`;
	}

	async function open(hit: SearchHit, idx: number | null) {
		sessionState.pendingOpen = { sessionId: hit.session_id, idx };
		await sessionState.selectSession(hit.session_id);
	}

	function clear() {
		query = '';
		hits = [];
	}
</script>

<div class="space-y-2">
	<div class="relative">
		<input
			type="search"
			bind:value={query}
			oninput={onInput}
			placeholder="Search transcripts…"
			class="w-full bg-gray-900 border border-gray-700 rounded px-2.5 py-1.5 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-gray-500"
		/>
		{#if query}
			<button onclick={clear} class="absolute right-2 top-1.5 text-gray-500 hover:text-gray-300 text-xs">✕</button>
		{/if}
	</div>

	{#if query.trim()}
		<div class="space-y-1 max-h-96 overflow-y-auto">
			{#if hits.length === 0 && !searching}
				<p class="text-xs text-gray-600 px-1">No matches</p>
			{/if}
			{#each hits as hit (hit.session_id)}
				<div class="rounded-lg border border-gray-800 bg-gray-900/60 p-2 space-y-1">
					<button onclick={() => open(hit, null)} class="w-full text-left text-sm font-medium text-gray-200 hover:text-white truncate">
						{hit.session_name}
					</button>
					{#if hit.session_snippet}
						<p class="text-[11px] text-gray-500 leading-4">
							{#each runs(hit.session_snippet) as r}{#if r.hit}<mark class="bg-yellow-800/60 text-yellow-100 rounded px-0.5">{r.text}</mark>{:else}{r.text}{/if}{/each}
						</p>
					{/if}
					{#each hit.segments as seg (seg.idx)}
						<button onclick={() => open(hit, seg.idx)} class="w-full text-left text-[11px] leading-4 text-gray-400 hover:text-gray-200 hover:bg-gray-800/60 rounded px-1">
							<span class="font-mono text-gray-600">{formatTime(seg.start)}</span>
							<span class="text-gray-500">{seg.speaker}:</span>
							{#each runs(seg.snippet) as r}{#if r.hit}<mark class="bg-yellow-800/60 text-yellow-100 rounded px-0.5">{r.text}</mark>{:else}{r.text}{/if}{/each}
						</button>
					{/each}
				</div>
			{/each}
		</div>
	{/if}
</div>
