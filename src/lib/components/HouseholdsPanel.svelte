<script lang="ts">
	import { deleteHousehold, getHousehold, listHouseholds, saveHousehold, syncHouseholdsFromHubSpot } from '$lib/api/backend.js';
	import { groupFacts } from '$lib/app/facts.js';
	import { connectionState } from '$lib/stores/connection.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { HouseholdDetail, HouseholdMember, HouseholdSummary } from '$lib/types/index.js';

	// Households (backend services/households.py): clients grouped as the firm serves them, with
	// what they said across their meetings.
	let { onOpenSession, initial = null }: { onOpenSession?: () => void; initial?: string | null } = $props();

	let households = $state<HouseholdSummary[]>([]);
	let loaded = $state(false);
	let selected = $state<string | null>(null);
	let detail = $state<HouseholdDetail | null>(null);
	let editing = $state<{ id: string | null; name: string; members: string } | null>(null);
	let syncing = $state(false);
	const isAdmin = $derived(connectionState.me?.role === 'admin');

	async function load() {
		try {
			households = await listHouseholds();
			loaded = true;
			if (!selected && households.length) selected = initial ?? households[0].id;
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not load households');
		}
	}

	$effect(() => {
		load();
	});

	$effect(() => {
		const id = selected;
		if (!id) {
			detail = null;
			return;
		}
		getHousehold(id)
			.then((d) => {
				if (selected === id) detail = d;
			})
			.catch(() => (detail = null));
	});

	// Members as lines: "Maria Lopez" or "Maria Lopez <maria@example.com>".
	const asLines = (members: HouseholdMember[]) => members.map((m) => (m.email ? `${m.name} <${m.email}>` : m.name)).join('\n');

	function parse(text: string, before: HouseholdMember[]) {
		return text
			.split('\n')
			.map((line) => line.match(/^\s*([^<]+?)\s*(?:<\s*([^>]*?)\s*>)?\s*$/))
			.filter((m): m is RegExpMatchArray => !!m && !!m[1].trim())
			.map((m) => {
				const name = m[1].trim();
				const was = before.find((b) => b.name.toLowerCase() === name.toLowerCase());
				return { name, email: m[2] ?? was?.email ?? '', source: was?.source ?? ('manual' as const) };
			});
	}

	async function save() {
		if (!editing) return;
		try {
			const before = editing.id && detail ? detail.members : [];
			const saved = await saveHousehold({ name: editing.name, members: parse(editing.members, before) }, editing.id ?? undefined);
			editing = null;
			selected = saved.id;
			await load();
			detail = await getHousehold(saved.id);
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not save the household');
		}
	}

	async function remove() {
		if (!detail || !confirm(`Remove the household “${detail.name}”? Its people and meetings stay.`)) return;
		try {
			await deleteHousehold(detail.id);
			selected = null;
			await load();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not remove the household');
		}
	}

	async function sync() {
		syncing = true;
		try {
			const r = await syncHouseholdsFromHubSpot();
			toastState.success(`HubSpot: ${r.created} new, ${r.updated} updated`);
			await load();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message.replace(/^\d+: /, '') : 'Could not read HubSpot');
		} finally {
			syncing = false;
		}
	}

	async function open(id: string) {
		onOpenSession?.();
		await sessionState.selectSession(id);
	}

	const day = (iso: string | null) =>
		iso ? new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }) : '—';
	const groups = $derived(groupFacts(detail?.facts ?? []));
</script>

<div class="grid gap-4 md:grid-cols-[14rem_1fr]">
	<div class="space-y-2">
		<div class="flex flex-wrap gap-2">
			<button onclick={() => (editing = { id: null, name: '', members: '' })} class="rounded border border-gray-700 bg-gray-800 px-2 py-1 text-xs text-gray-200 hover:bg-gray-700">
				New household
			</button>
			{#if isAdmin}
				<button onclick={sync} disabled={syncing} class="text-xs text-blue-400 hover:text-blue-300 disabled:opacity-50" title="Companies and their contacts become households; nothing is written to HubSpot">
					{syncing ? 'Reading HubSpot…' : 'From HubSpot'}
				</button>
			{/if}
		</div>
		{#if loaded && households.length === 0}
			<p class="text-xs text-gray-600">No households yet. Group the people you meet together (a couple, a family).</p>
		{/if}
		<ul class="space-y-0.5 max-h-[60vh] overflow-y-auto">
			{#each households as h (h.id)}
				<li>
					<button
						onclick={() => ((selected = h.id), (editing = null))}
						class="w-full text-left rounded px-2 py-1 text-sm {selected === h.id ? 'bg-gray-800 text-gray-100' : 'text-gray-300 hover:bg-gray-900'}"
					>
						<span class="font-medium">{h.name}</span>
						<span class="block text-[11px] text-gray-500">
							{h.members.length} {h.members.length === 1 ? 'person' : 'people'} · {h.meetings} meeting{h.meetings === 1 ? '' : 's'}{#if h.last_meeting} · {day(h.last_meeting)}{/if}
						</span>
					</button>
				</li>
			{/each}
		</ul>
	</div>

	{#if editing}
		<form class="space-y-3" onsubmit={(e) => (e.preventDefault(), save())} aria-label="Household">
			<label class="block text-xs text-gray-400">
				Name
				<input bind:value={editing.name} required placeholder="Lopez household" class="mt-1 block w-full rounded border border-gray-700 bg-gray-800 px-2 py-1 text-sm text-gray-100" />
			</label>
			<label class="block text-xs text-gray-400">
				People, one per line, as they are named in meetings (an email helps match calendar invites)
				<textarea bind:value={editing.members} rows="5" placeholder={'Maria Lopez <maria@example.com>\nDavid Lopez'} class="mt-1 block w-full rounded border border-gray-700 bg-gray-800 px-2 py-1 text-sm text-gray-100"></textarea>
			</label>
			<div class="flex gap-2">
				<button type="submit" class="rounded bg-blue-600 px-3 py-1.5 text-sm text-white hover:bg-blue-500">Save household</button>
				<button type="button" onclick={() => (editing = null)} class="px-3 py-1.5 text-sm text-gray-400 hover:text-gray-200">Cancel</button>
			</div>
		</form>
	{:else if detail}
		<article class="space-y-4" aria-label="Household {detail.name}">
			<header>
				<div class="flex flex-wrap items-baseline gap-2">
					<h3 class="text-lg font-semibold text-gray-100">{detail.name}</h3>
					{#if detail.hubspot_company_id}<span class="text-xs text-gray-500">from HubSpot</span>{/if}
					<button onclick={() => detail && (editing = { id: detail.id, name: detail.name, members: asLines(detail.members) })} class="ml-auto text-xs text-blue-400 hover:text-blue-300">Edit</button>
					<button onclick={remove} class="text-xs text-gray-500 hover:text-red-400">Remove</button>
				</div>
				<p class="text-xs text-gray-400 mt-0.5">
					{detail.members.map((m) => m.name).join(', ') || 'Nobody yet'} · {detail.meetings.length} meeting{detail.meetings.length === 1 ? '' : 's'}
				</p>
			</header>

			{#if groups.length}
				<section>
					<h4 class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-1">What they have told you</h4>
					<p class="text-[11px] text-gray-600 mb-2">From meeting summaries, newest first: check the meeting before relying on one.</p>
					<div class="space-y-2">
						{#each groups as g (g.kind)}
							<div>
								<p class="text-xs text-gray-500">{g.heading}</p>
								<ul class="space-y-0.5">
									{#each g.facts as f, i (f.session_id + i)}
										<li class="text-sm text-gray-300">
											{f.text}
											<button onclick={() => open(f.session_id)} class="text-xs text-gray-500 hover:text-gray-300">· {day(f.created_at)}</button>
										</li>
									{/each}
								</ul>
							</div>
						{/each}
					</div>
				</section>
			{/if}

			{#if detail.open_tasks.length}
				<section>
					<h4 class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-1">Open tasks</h4>
					<ul class="space-y-0.5">
						{#each detail.open_tasks as t (t.session_id + t.idx)}
							<li class="text-sm text-gray-300">○ {t.text}{#if t.owner}<span class="text-gray-500"> · {t.owner}</span>{/if}</li>
						{/each}
					</ul>
				</section>
			{/if}

			<section>
				<h4 class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-1">Meetings</h4>
				{#if detail.meetings.length === 0}
					<p class="text-xs text-gray-600">None yet: meetings where these people speak or are invited, or that name them in the title, show up here.</p>
				{/if}
				<ul class="space-y-0.5">
					{#each detail.meetings as m (m.id)}
						<li>
							<button onclick={() => open(m.id)} class="text-left text-sm text-gray-300 hover:text-gray-100">
								{m.name} <span class="text-xs text-gray-500">· {day(m.created_at)}</span>
							</button>
						</li>
					{/each}
				</ul>
			</section>
		</article>
	{/if}
</div>
