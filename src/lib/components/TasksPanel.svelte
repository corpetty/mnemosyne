<script lang="ts">
	import { listActionItems, setActionItemDone } from '$lib/api/backend.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { wsState } from '$lib/stores/websocket.svelte.js';
	import type { TaskItem } from '$lib/types/index.js';

	let { onOpenSession }: { onOpenSession?: () => void } = $props();

	let items = $state<TaskItem[]>([]);
	let loaded = $state(false);
	let status = $state<'open' | 'done' | 'all'>('open');
	let owner = $state('');
	let query = $state('');

	async function load() {
		try {
			items = await listActionItems('all');
			loaded = true;
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not load tasks');
		}
	}

	$effect(() => {
		load();
		// A summary landing or an item toggled elsewhere changes the list.
		return wsState.onMessage((raw) => {
			const msg = raw as { type?: string };
			if (msg.type === 'session') load();
		});
	});

	async function toggle(t: TaskItem) {
		const done = !t.done;
		t.done = done; // optimistic
		try {
			await setActionItemDone(t.session_id, t.idx, done);
		} catch (e) {
			t.done = !done;
			toastState.error(e instanceof Error ? e.message : 'Could not update the task');
		}
	}

	async function open(t: TaskItem) {
		sessionState.pendingOpen = null;
		onOpenSession?.();
		await sessionState.selectSession(t.session_id);
	}

	async function openLink(href: string) {
		try {
			const { invoke } = await import('@tauri-apps/api/core');
			await invoke('plugin:shell|open', { path: href });
		} catch {
			window.open(href, '_blank', 'noopener,noreferrer');
		}
	}

	const owners = $derived([...new Set(items.map((t) => t.owner).filter((o): o is string => !!o))].sort());
	const shown = $derived(
		items.filter(
			(t) =>
				(status === 'all' || (status === 'done') === t.done) &&
				(!owner || (owner === '—' ? !t.owner : t.owner === owner)) &&
				(!query.trim() || t.text.toLowerCase().includes(query.trim().toLowerCase()))
		)
	);
	const openCount = $derived(items.filter((t) => !t.done).length);
	// Group by meeting, keeping the newest-first order from the backend.
	const groups = $derived.by(() => {
		const out: { id: string; name: string; date: string; items: TaskItem[] }[] = [];
		for (const t of shown) {
			let g = out.find((x) => x.id === t.session_id);
			if (!g) {
				g = {
					id: t.session_id,
					name: t.session_name,
					date: new Date(t.created_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }),
					items: []
				};
				out.push(g);
			}
			g.items.push(t);
		}
		return out;
	});
</script>

<div class="space-y-5">
	<div>
		<h2 class="text-xl font-semibold text-gray-100 mb-1">Tasks</h2>
		<p class="text-xs text-gray-500">
			Action items from every summarized meeting · {openCount} open. Ticking one off is saved with the meeting, kept when
			it is summarized again, and exported to Obsidian as <code>- [x]</code>.
		</p>
	</div>

	<div class="flex flex-wrap items-center gap-2 text-sm">
		<div class="flex rounded border border-gray-700 overflow-hidden text-xs">
			{#each ['open', 'done', 'all'] as const as s}
				<button
					onclick={() => (status = s)}
					class="px-2.5 py-1 capitalize {status === s ? 'bg-gray-700 text-gray-100' : 'text-gray-400 hover:bg-gray-800'}"
				>{s}</button>
			{/each}
		</div>
		<select bind:value={owner} class="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200" aria-label="Owner">
			<option value="">Anyone</option>
			{#each owners as o}<option value={o}>{o}</option>{/each}
			<option value="—">No owner</option>
		</select>
		<input
			type="search"
			bind:value={query}
			placeholder="Filter…"
			class="bg-gray-900 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200 placeholder-gray-600 w-40"
		/>
	</div>

	{#each groups as g (g.id)}
		<section class="space-y-1">
			<button onclick={() => open(g.items[0])} class="text-xs text-gray-500 hover:text-gray-300">
				<span class="font-medium text-gray-300">{g.name}</span> · {g.date}
			</button>
			<ul class="space-y-0.5">
				{#each g.items as t (t.idx)}
					<li class="flex items-start gap-2 text-sm rounded px-1 py-0.5 hover:bg-gray-900/60">
						<input
							type="checkbox"
							checked={t.done}
							onchange={() => toggle(t)}
							aria-label="Done: {t.text}"
							class="mt-1 rounded border-gray-600 bg-gray-800"
						/>
						<span class={t.done ? 'line-through text-gray-500' : 'text-gray-200'}>{t.text}</span>
						{#if t.owner}<span class="text-xs text-gray-500 pt-0.5 whitespace-nowrap">· {t.owner}</span>{/if}
						{#if t.issue_url}
							<button onclick={() => openLink(t.issue_url!)} class="text-xs text-green-500 hover:text-green-300 pt-0.5">issue</button>
						{/if}
					</li>
				{/each}
			</ul>
		</section>
	{/each}

	{#if loaded && shown.length === 0}
		<p class="text-sm text-gray-600">
			{items.length === 0 ? 'No action items yet. They come from meeting summaries.' : 'Nothing matches.'}
		</p>
	{/if}
</div>
