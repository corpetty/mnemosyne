<script lang="ts">
	import { listActionItems, setActionItemDone, updateActionItem } from '$lib/api/backend.js';
	import { connectionState } from '$lib/stores/connection.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { wsState } from '$lib/stores/websocket.svelte.js';
	import type { TaskItem } from '$lib/types/index.js';

	let { onOpenSession }: { onOpenSession?: () => void } = $props();

	let items = $state<TaskItem[]>([]);
	let loaded = $state(false);
	let status = $state<'open' | 'done' | 'all'>('open');
	// Yours first on a team server (the backend marks the items whose owner is you).
	const MINE = '__mine__';
	let owner = $state(connectionState.me?.team_mode ? MINE : '');
	let query = $state('');
	let dueFilter = $state<'' | 'overdue' | 'week'>('');

	const today = new Date().toISOString().slice(0, 10);
	const weekOut = new Date(Date.now() + 7 * 86400_000).toISOString().slice(0, 10);
	function dueLabel(due: string): string {
		return new Date(`${due}T12:00:00`).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
	}
	function dueClass(t: TaskItem): string {
		if (!t.due || t.done) return 'text-gray-500';
		if (t.due < today) return 'text-red-400';
		return t.due <= weekOut ? 'text-amber-300' : 'text-gray-400';
	}

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

	// One task at a time is edited in place (text, owner, due date).
	const keyOf = (t: TaskItem) => `${t.session_id}:${t.idx}`;
	let editing = $state<string | null>(null);
	let draft = $state({ text: '', owner: '', due: '' });
	let saving = $state(false);

	function startEdit(t: TaskItem) {
		editing = keyOf(t);
		draft = { text: t.text, owner: t.owner ?? '', due: t.due ?? '' };
	}

	async function saveEdit(t: TaskItem) {
		if (saving || !draft.text.trim()) return;
		saving = true;
		try {
			const got = await updateActionItem(t.session_id, t.idx, {
				text: draft.text,
				owner: draft.owner.trim() || null,
				due: draft.due || null
			});
			Object.assign(t, { text: got.text, owner: got.owner, due: got.due });
			editing = null;
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not save the task');
		} finally {
			saving = false;
		}
	}

	function editKey(e: KeyboardEvent, t: TaskItem) {
		if (e.key === 'Enter') saveEdit(t);
		else if (e.key === 'Escape') editing = null;
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
				(!owner || (owner === MINE ? t.mine : owner === '—' ? !t.owner : t.owner === owner)) &&
				(!query.trim() || t.text.toLowerCase().includes(query.trim().toLowerCase())) &&
				(!dueFilter || (!!t.due && (dueFilter === 'overdue' ? t.due < today : t.due <= weekOut)))
		)
	);
	// Open tasks with a deadline, soonest first, above the per-meeting lists.
	const deadlines = $derived(
		shown.filter((t) => t.due && !t.done).sort((a, b) => (a.due! < b.due! ? -1 : a.due! > b.due! ? 1 : 0))
	);
	const openCount = $derived(items.filter((t) => !t.done).length);
	// Group by meeting, keeping the newest-first order from the backend.
	const groups = $derived.by(() => {
		const out: { id: string; name: string; date: string; items: TaskItem[] }[] = [];
		for (const t of shown) {
			if (t.due && !t.done) continue; // listed under Deadlines
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

{#snippet pencil(t: TaskItem)}
	{#if t.can_edit}
		<button
			onclick={() => startEdit(t)}
			class="opacity-0 group-hover:opacity-100 focus:opacity-100 text-xs text-gray-500 hover:text-gray-200 pt-0.5"
			aria-label="Edit task: {t.text}"
			title="Edit the text, owner or due date"
		>✎</button>
	{/if}
{/snippet}

{#snippet editor(t: TaskItem)}
	<li class="flex flex-wrap items-center gap-1.5 text-sm rounded px-1 py-1 bg-gray-900">
		<!-- svelte-ignore a11y_autofocus -->
		<input
			bind:value={draft.text}
			onkeydown={(e) => editKey(e, t)}
			autofocus
			aria-label="Task text"
			class="flex-1 min-w-[12rem] bg-gray-950 border border-gray-700 rounded px-2 py-1 text-sm text-gray-200"
		/>
		<input
			bind:value={draft.owner}
			onkeydown={(e) => editKey(e, t)}
			placeholder="Owner"
			aria-label="Task owner"
			class="w-32 bg-gray-950 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200"
		/>
		<input
			type="date"
			bind:value={draft.due}
			onkeydown={(e) => editKey(e, t)}
			aria-label="Task due date"
			class="w-36 bg-gray-950 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200"
		/>
		<button onclick={() => saveEdit(t)} disabled={saving || !draft.text.trim()} class="px-2 py-1 text-xs rounded bg-blue-600 hover:bg-blue-700 text-white disabled:opacity-50">Save</button>
		<button onclick={() => (editing = null)} class="px-2 py-1 text-xs rounded bg-gray-800 border border-gray-700 text-gray-300">Cancel</button>
	</li>
{/snippet}

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
			<option value={MINE}>Mine</option>
			{#each owners as o}<option value={o}>{o}</option>{/each}
			<option value="—">No owner</option>
		</select>
		<select bind:value={dueFilter} class="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200" aria-label="Deadline">
			<option value="">Any deadline</option>
			<option value="overdue">Overdue</option>
			<option value="week">Due within a week</option>
		</select>
		<input
			type="search"
			bind:value={query}
			placeholder="Filter…"
			class="bg-gray-900 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200 placeholder-gray-600 w-40"
		/>
	</div>

	{#if deadlines.length}
		<section class="space-y-1" aria-label="Deadlines">
			<h3 class="text-xs font-medium text-gray-300">Deadlines</h3>
			<ul class="space-y-0.5">
				{#each deadlines as t (t.session_id + t.idx)}
					{#if editing === keyOf(t)}
						{@render editor(t)}
					{:else}
					<li class="group flex items-start gap-2 text-sm rounded px-1 py-0.5 hover:bg-gray-900/60">
						<input type="checkbox" checked={t.done} onchange={() => toggle(t)} aria-label="Done: {t.text}" class="mt-1 rounded border-gray-600 bg-gray-800" />
						<span class="w-14 shrink-0 text-xs pt-0.5 {dueClass(t)}" title={t.due! < today ? 'Overdue' : 'Due'}>{dueLabel(t.due!)}</span>
						<span class="text-gray-200">{t.text}</span>
						{#if t.owner}<span class="text-xs text-gray-500 pt-0.5 whitespace-nowrap">· {t.owner}</span>{/if}
						{@render pencil(t)}
						<button onclick={() => open(t)} class="ml-auto text-xs text-gray-600 hover:text-gray-300 pt-0.5 whitespace-nowrap">{t.session_name}</button>
					</li>
					{/if}
				{/each}
			</ul>
		</section>
	{/if}

	{#each groups as g (g.id)}
		<section class="space-y-1">
			<button onclick={() => open(g.items[0])} class="text-xs text-gray-500 hover:text-gray-300">
				<span class="font-medium text-gray-300">{g.name}</span> · {g.date}
			</button>
			<ul class="space-y-0.5">
				{#each g.items as t (t.idx)}
					{#if editing === keyOf(t)}
						{@render editor(t)}
					{:else}
					<li class="group flex items-start gap-2 text-sm rounded px-1 py-0.5 hover:bg-gray-900/60">
						<input
							type="checkbox"
							checked={t.done}
							onchange={() => toggle(t)}
							aria-label="Done: {t.text}"
							class="mt-1 rounded border-gray-600 bg-gray-800"
						/>
						<span class={t.done ? 'line-through text-gray-500' : 'text-gray-200'}>{t.text}</span>
						{#if t.owner}<span class="text-xs text-gray-500 pt-0.5 whitespace-nowrap">· {t.owner}</span>{/if}
						{#if t.due}<span class="text-xs pt-0.5 whitespace-nowrap {dueClass(t)}">· due {dueLabel(t.due)}</span>{/if}
						{#if t.issue_url}
							<button onclick={() => openLink(t.issue_url!)} class="text-xs text-green-500 hover:text-green-300 pt-0.5">issue</button>
						{/if}
						{@render pencil(t)}
					</li>
					{/if}
				{/each}
			</ul>
		</section>
	{/each}

	{#if loaded && shown.length === 0}
		<p class="text-sm text-gray-600">
			{items.length === 0
				? 'No action items yet. They come from meeting summaries.'
				: owner === MINE
					? 'Nothing is given to you. Choose Anyone to see every action item.'
					: 'Nothing matches.'}
		</p>
	{/if}
</div>
