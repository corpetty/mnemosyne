<script lang="ts">
	import {
		applyTaskSuggestion,
		dismissTaskSuggestion,
		linkTasks,
		listTaskSuggestions,
		listTasks,
		unlinkTaskMention,
		updateActionItem,
		updateTask
	} from '$lib/api/backend.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { wsState } from '$lib/stores/websocket.svelte.js';
	import type { TaskMention, TaskObject, TaskSuggestion } from '$lib/types/index.js';

	let { onOpenSession }: { onOpenSession?: () => void } = $props();

	// One entry per task (linked action items of several meetings are one); its head is the mention in
	// the newest meeting, whose text, owner and due date the row shows.
	let items = $state<TaskObject[]>([]);
	let suggestions = $state<TaskSuggestion[]>([]);
	let loaded = $state(false);
	let status = $state<'open' | 'done' | 'all'>('open');
	// Only what is yours by default (the backend marks the items whose owner is you); the ones you
	// star on other people's lists are "Following"; "Everyone" is the whole list.
	type Scope = 'mine' | 'following' | 'everyone';
	let scope = $state<Scope>('mine');
	let owner = $state('');
	let query = $state('');
	let dueFilter = $state<'' | 'overdue' | 'week'>('');

	const today = new Date().toISOString().slice(0, 10);
	const weekOut = new Date(Date.now() + 7 * 86400_000).toISOString().slice(0, 10);
	function dueLabel(due: string): string {
		return new Date(`${due}T12:00:00`).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
	}
	function dueClass(t: TaskObject): string {
		if (!t.due || t.done) return 'text-gray-500';
		if (t.due < today) return 'text-red-400';
		return t.due <= weekOut ? 'text-amber-300' : 'text-gray-400';
	}

	async function load() {
		try {
			items = await listTasks('all');
			loaded = true;
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not load tasks');
		}
		try {
			suggestions = await listTaskSuggestions();
		} catch {
			suggestions = []; // the list is the point; suggestions are a bonus
		}
	}

	const head = (t: TaskObject) => t.mentions[0];
	const mentionDate = (m: TaskMention) =>
		new Date(m.created_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });

	$effect(() => {
		load();
		// A summary landing or an item toggled elsewhere changes the list.
		return wsState.onMessage((raw) => {
			const msg = raw as { type?: string };
			if (msg.type === 'session') load();
		});
	});

	async function toggle(t: TaskObject) {
		const done = !t.done;
		t.done = done; // optimistic
		try {
			await updateTask(t.key, { done });
		} catch (e) {
			t.done = !done;
			toastState.error(e instanceof Error ? e.message : 'Could not update the task');
		}
	}

	async function toggleFollow(t: TaskObject) {
		const follow = !t.follow;
		t.follow = follow; // optimistic
		try {
			await updateTask(t.key, { follow });
		} catch (e) {
			t.follow = !follow;
			toastState.error(e instanceof Error ? e.message : 'Could not update the task');
		}
	}

	// One task at a time is edited in place (text, owner, due date).
	let editing = $state<string | null>(null);
	let draft = $state({ text: '', owner: '', due: '' });
	let saving = $state(false);

	function startEdit(t: TaskObject) {
		editing = t.key;
		draft = { text: t.text, owner: t.owner ?? '', due: t.due ?? '' };
	}

	async function saveEdit(t: TaskObject) {
		if (saving || !draft.text.trim()) return;
		saving = true;
		try {
			// Editing a task edits its head mention, the meeting the row shows.
			const h = head(t);
			const got = await updateActionItem(h.session_id, h.idx, {
				text: draft.text,
				owner: draft.owner.trim() || null,
				due: draft.due || null
			});
			Object.assign(t, { text: got.text, owner: got.owner, due: got.due });
			Object.assign(h, { text: got.text, owner: got.owner, due: got.due });
			editing = null;
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not save the task');
		} finally {
			saving = false;
		}
	}

	function editKey(e: KeyboardEvent, t: TaskObject) {
		if (e.key === 'Enter') saveEdit(t);
		else if (e.key === 'Escape') editing = null;
	}

	async function open(sessionId: string) {
		sessionState.pendingOpen = null;
		onOpenSession?.();
		await sessionState.selectSession(sessionId);
	}

	// "in N meetings": the mentions of a linked task, unlinkable one by one.
	let expanded = $state<Record<string, boolean>>({});

	async function unlink(t: TaskObject, m: TaskMention) {
		try {
			await unlinkTaskMention(t.key, m);
			await load();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not unlink the task');
		}
	}

	// Link a task to an earlier one: an inline picker under the row.
	let picking = $state<string | null>(null);
	let pickQuery = $state('');
	let pickDone = $state(false);

	function startPick(t: TaskObject) {
		picking = t.key;
		pickQuery = '';
		pickDone = false;
	}

	function candidates(t: TaskObject): TaskObject[] {
		const q = pickQuery.trim().toLowerCase();
		return items
			.filter((o) => o.key !== t.key && (pickDone || !o.done) && (!q || o.text.toLowerCase().includes(q)))
			.sort((a, b) => Number(a.done) - Number(b.done)) // open ones first (stable)
			.slice(0, 8);
	}

	async function pick(t: TaskObject, other: TaskObject) {
		try {
			await linkTasks(head(t), head(other));
			picking = null;
			await load();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not link the tasks');
		}
	}

	let showSuggestions = $state(false);

	async function resolve(s: TaskSuggestion, apply: boolean) {
		try {
			if (apply) await applyTaskSuggestion(s);
			else await dismissTaskSuggestion(s);
			await load();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not update the suggestion');
		}
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
				(scope === 'everyone' || (scope === 'mine' ? t.mine : t.follow && !t.mine)) &&
				(scope !== 'everyone' || !owner || (owner === '—' ? !t.owner : t.owner === owner)) &&
				(!query.trim() || t.text.toLowerCase().includes(query.trim().toLowerCase())) &&
				(!dueFilter || (!!t.due && (dueFilter === 'overdue' ? t.due < today : t.due <= weekOut)))
		)
	);
	// Open tasks with a deadline, soonest first, above the per-meeting lists.
	const deadlines = $derived(
		shown.filter((t) => t.due && !t.done).sort((a, b) => (a.due! < b.due! ? -1 : a.due! > b.due! ? 1 : 0))
	);
	// What you have to do plus what you wait on; the rest is other people's.
	const openCount = $derived(items.filter((t) => !t.done && (t.mine || t.follow)).length);
	const allOpen = $derived(items.filter((t) => !t.done).length);
	const counts = $derived({
		mine: items.filter((t) => !t.done && t.mine).length,
		following: items.filter((t) => !t.done && t.follow && !t.mine).length,
		everyone: allOpen
	});
	// Group by meeting, keeping the newest-first order from the backend.
	const groups = $derived.by(() => {
		const out: { id: string; name: string; date: string; items: TaskObject[] }[] = [];
		for (const t of shown) {
			if (t.due && !t.done) continue; // listed under Deadlines
			const h = head(t);
			let g = out.find((x) => x.id === h.session_id);
			if (!g) {
				g = {
					id: h.session_id,
					name: h.session_name,
					date: mentionDate(h),
					items: []
				};
				out.push(g);
			}
			g.items.push(t);
		}
		return out;
	});
</script>

{#snippet linkedToggle(t: TaskObject)}
	{#if t.linked}
		<button
			onclick={() => (expanded[t.key] = !expanded[t.key])}
			aria-expanded={!!expanded[t.key]}
			class="text-xs text-gray-500 hover:text-gray-300 pt-0.5 whitespace-nowrap"
			title="The same task came up in these meetings"
		>in {t.mentions.length} meetings</button>
	{/if}
{/snippet}

{#snippet linkButton(t: TaskObject)}
	<button
		onclick={() => (picking === t.key ? (picking = null) : startPick(t))}
		class="opacity-0 group-hover:opacity-100 focus:opacity-100 text-xs text-gray-500 hover:text-gray-200 pt-0.5"
		aria-label="Link to an earlier task: {t.text}"
		title="Link to an earlier task"
	>link</button>
{/snippet}

{#snippet details(t: TaskObject)}
	{#if t.linked && expanded[t.key]}
		<li class="ml-7 pb-1">
			<ul class="space-y-0.5 text-xs text-gray-500" aria-label="Meetings of: {t.text}">
				{#each t.mentions as m, i (m.session_id + m.item_id)}
					<li class="flex flex-wrap items-baseline gap-x-1.5">
						<span>{mentionDate(m)} ·</span>
						<button onclick={() => open(m.session_id)} class="hover:text-gray-300">{m.session_name}</button>
						{#if i === 0}<span>(latest)</span>{/if}
						<span>— “{m.text}”</span>
						{#if i > 0 && (m.owner !== t.owner || m.due !== t.due)}
							<span>({[m.owner !== t.owner ? (m.owner ?? 'no owner') : '', m.due !== t.due ? (m.due ? `due ${dueLabel(m.due)}` : 'no due date') : ''].filter(Boolean).join(', ')})</span>
						{/if}
						<button onclick={() => unlink(t, m)} class="text-gray-500 hover:text-red-400 underline" aria-label="Unlink {m.session_name}: {m.text}">Unlink</button>
					</li>
				{/each}
			</ul>
		</li>
	{/if}
	{#if picking === t.key}
		<li class="ml-7 pb-1 space-y-1 text-xs">
			<div class="flex items-center gap-2">
				<!-- svelte-ignore a11y_autofocus -->
				<input
					bind:value={pickQuery}
					onkeydown={(e) => e.key === 'Escape' && (picking = null)}
					autofocus
					placeholder="Find a task…"
					aria-label="Find a task to link to"
					class="w-64 bg-gray-950 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200 placeholder-gray-600"
				/>
				<label class="flex items-center gap-1 text-gray-400">
					<input type="checkbox" bind:checked={pickDone} class="rounded border-gray-600 bg-gray-800" /> Show done
				</label>
				<button onclick={() => (picking = null)} class="px-2 py-1 rounded bg-gray-800 border border-gray-700 text-gray-300">Cancel</button>
			</div>
			<ul class="space-y-0.5">
				{#each candidates(t) as o (o.key)}
					<li>
						<button onclick={() => pick(t, o)} class="text-left text-gray-300 hover:text-gray-100 hover:bg-gray-900 rounded px-1 py-0.5">
							{o.text} · {head(o).session_name} · {mentionDate(head(o))}
						</button>
					</li>
				{:else}
					<li class="text-gray-600">No task matches.</li>
				{/each}
			</ul>
		</li>
	{/if}
{/snippet}

{#snippet pencil(t: TaskObject)}
	{#if t.can_edit}
		<button
			onclick={() => startEdit(t)}
			class="opacity-0 group-hover:opacity-100 focus:opacity-100 text-xs text-gray-500 hover:text-gray-200 pt-0.5"
			aria-label="Edit task: {t.text}"
			title="Edit the text, owner or due date"
		>✎</button>
	{/if}
{/snippet}

{#snippet star(t: TaskObject)}
	{#if !t.mine}
		<button
			onclick={() => toggleFollow(t)}
			class="text-xs pt-0.5 {t.follow ? 'text-amber-300 hover:text-amber-200' : 'opacity-0 group-hover:opacity-100 focus:opacity-100 text-gray-500 hover:text-amber-300'}"
			aria-label="{t.follow ? 'Stop following' : 'Follow'}: {t.text}"
			aria-pressed={t.follow}
			title={t.follow ? 'Following: shown under Following. Click to stop' : 'Follow: wait on this one, shown under Following'}
		>{t.follow ? '★' : '☆'}</button>
	{/if}
{/snippet}

{#snippet editor(t: TaskObject)}
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
			Action items from your summarized meetings · {openCount} open for you ({allOpen} in all). Ticking one off is saved with the meeting, kept when
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
		<div class="flex rounded border border-gray-700 overflow-hidden text-xs" role="group" aria-label="Whose tasks">
			{#each [['mine', 'Mine'], ['following', 'Following'], ['everyone', 'Everyone']] as const as [s, label]}
				<button
					onclick={() => (scope = s)}
					aria-pressed={scope === s}
					title={s === 'mine' ? 'Items given to you' : s === 'following' ? 'Other people\'s items you starred' : 'Every action item'}
					class="px-2.5 py-1 {scope === s ? 'bg-gray-700 text-gray-100' : 'text-gray-400 hover:bg-gray-800'}"
				>{label}{#if loaded && s !== 'everyone' && counts[s]} <span class="text-gray-500">{counts[s]}</span>{/if}</button>
			{/each}
		</div>
		{#if scope === 'everyone'}
			<select bind:value={owner} class="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200" aria-label="Owner">
				<option value="">Anyone</option>
				{#each owners as o}<option value={o}>{o}</option>{/each}
				<option value="—">No owner</option>
			</select>
		{/if}
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

	{#if suggestions.length}
		<section class="space-y-1" aria-label="Suggested links">
			<button
				onclick={() => (showSuggestions = !showSuggestions)}
				aria-expanded={showSuggestions}
				class="text-xs font-medium text-gray-300 hover:text-gray-100"
			>{showSuggestions ? '▾' : '▸'} Suggested links ({suggestions.length})</button>
			{#if showSuggestions}
				<ul class="space-y-2">
					{#each suggestions as s (s.a.session_id + s.a.item_id + s.b.session_id + s.b.item_id)}
						<li class="rounded bg-gray-900/60 px-2 py-1.5 text-xs space-y-1">
							{#each [s.a, s.b] as m (m.session_id + m.item_id)}
								<p class="text-gray-300">{m.text} <span class="text-gray-500">· {m.session_name} · {mentionDate(m)}</span></p>
							{/each}
							<div class="flex gap-2">
								<button onclick={() => resolve(s, true)} class="px-2 py-0.5 rounded bg-blue-600 hover:bg-blue-700 text-white">Same task</button>
								<button onclick={() => resolve(s, false)} class="px-2 py-0.5 rounded bg-gray-800 border border-gray-700 text-gray-300">Not the same</button>
							</div>
						</li>
					{/each}
				</ul>
			{/if}
		</section>
	{/if}

	{#if deadlines.length}
		<section class="space-y-1" aria-label="Deadlines">
			<h3 class="text-xs font-medium text-gray-300">Deadlines</h3>
			<ul class="space-y-0.5">
				{#each deadlines as t (t.key)}
					{#if editing === t.key}
						{@render editor(t)}
					{:else}
					<li class="group flex items-start gap-2 text-sm rounded px-1 py-0.5 hover:bg-gray-900/60">
						<input type="checkbox" checked={t.done} onchange={() => toggle(t)} aria-label="Done: {t.text}" class="mt-1 rounded border-gray-600 bg-gray-800" />
						<span class="w-14 shrink-0 text-xs pt-0.5 {dueClass(t)}" title={t.due! < today ? 'Overdue' : 'Due'}>{dueLabel(t.due!)}</span>
						<span class="text-gray-200">{t.text}</span>
						{#if t.owner}<span class="text-xs text-gray-500 pt-0.5 whitespace-nowrap">· {t.owner}</span>{/if}
						{@render star(t)}
						{@render pencil(t)}
						{@render linkButton(t)}
						<button onclick={() => open(head(t).session_id)} class="ml-auto text-xs text-gray-600 hover:text-gray-300 pt-0.5 whitespace-nowrap">{head(t).session_name}</button>
						{@render linkedToggle(t)}
					</li>
						{@render details(t)}
					{/if}
				{/each}
			</ul>
		</section>
	{/if}

	{#each groups as g (g.id)}
		<section class="space-y-1">
			<button onclick={() => open(g.id)} class="text-xs text-gray-500 hover:text-gray-300">
				<span class="font-medium text-gray-300">{g.name}</span> · {g.date}
			</button>
			<ul class="space-y-0.5">
				{#each g.items as t (t.key)}
					{#if editing === t.key}
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
						{@render star(t)}
						{@render pencil(t)}
						{@render linkButton(t)}
						{@render linkedToggle(t)}
					</li>
						{@render details(t)}
					{/if}
				{/each}
			</ul>
		</section>
	{/each}

	{#if loaded && shown.length === 0}
		<p class="text-sm text-gray-600">
			{items.length === 0
				? 'No action items yet. They come from meeting summaries.'
				: scope === 'mine' && !query.trim() && !dueFilter
					? status === 'open'
						? 'Nothing is open for you. Other people\'s items are under Everyone; star one (☆) to follow it under Following.'
						: 'Nothing here for you yet.'
					: scope === 'following' && !query.trim() && !dueFilter
						? 'You are not following anything. Star (☆) an item under Everyone to wait on it here.'
						: 'Nothing matches.'}
		</p>
	{/if}
</div>
