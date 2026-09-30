<script lang="ts">
	import { getHubSpotMatches, getHubSpotState, pushToHubSpot } from '$lib/api/backend.js';
	import { jobsState } from '$lib/stores/jobs.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { HubSpotContact, HubSpotState } from '$lib/types/index.js';

	// The meeting in HubSpot (backend services/hubspot.py): the first push asks which contacts it
	// belongs to; later pushes update the same records with those contacts.
	let hs = $state<HubSpotState | null>(null);
	let candidates = $state<HubSpotContact[] | null>(null);
	let searched = $state<string[]>([]);
	let chosen = $state<Set<string>>(new Set());
	let picking = $state(false);
	let loading = $state(false);
	let pushing = $state(false);
	let loadedFor = '';

	const session = $derived(sessionState.activeSession);

	$effect(() => {
		const id = session?.id ?? '';
		if (id === loadedFor) return;
		loadedFor = id;
		hs = null;
		picking = false;
		candidates = null;
		if (id)
			getHubSpotState(id)
				.then((s) => {
					if (loadedFor === id) hs = s;
				})
				.catch(() => {});
	});

	// A summary may have updated HubSpot on its own (hubspot_auto_push).
	$effect(() =>
		jobsState.onComplete((job) => {
			if (job.kind === 'summarize' && job.session_id === loadedFor)
				getHubSpotState(loadedFor)
					.then((s) => {
						if (job.session_id === loadedFor) hs = s;
					})
					.catch(() => {});
		})
	);

	const pushed = $derived(!!hs?.pushed_at);

	async function openPicker() {
		if (!session) return;
		const id = session.id;
		picking = true;
		loading = true;
		candidates = null;
		try {
			const r = await getHubSpotMatches(id);
			if (loadedFor !== id) return;
			candidates = r.candidates;
			searched = r.searched;
			hs = r.state;
			const confirmed = r.state.contacts.map((c) => c.id);
			// Confirmed contacts stay chosen; on a first push, suggest those found by email.
			chosen = new Set(confirmed.length ? confirmed : r.candidates.filter((c) => c.matched_by === 'email').map((c) => c.id));
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not look up HubSpot contacts');
			picking = false;
		} finally {
			loading = false;
		}
	}

	function toggle(id: string) {
		const next = new Set(chosen);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		chosen = next;
	}

	async function push(ids: string[]) {
		if (!session || !ids.length) return;
		const id = session.id;
		pushing = true;
		try {
			const r = await pushToHubSpot(id, ids);
			if (loadedFor === id) hs = r.state;
			picking = false;
			toastState.success(r.message);
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not send to HubSpot');
		} finally {
			pushing = false;
		}
	}

	function update() {
		const ids = hs?.contacts.map((c) => c.id) ?? [];
		if (!ids.length) return openPicker();
		push(ids);
	}
</script>

<section class="bg-gray-900 border border-gray-700 rounded-lg p-3 space-y-2">
	<div class="flex flex-wrap items-center gap-2">
		<h4 class="text-xs font-semibold text-gray-400 uppercase tracking-wide mr-auto">HubSpot</h4>
		{#if pushed}
			<button onclick={openPicker} disabled={pushing || loading} class="px-3 py-1 text-xs rounded text-gray-400 hover:text-gray-200 disabled:opacity-50">
				Change contacts
			</button>
			<button onclick={update} disabled={pushing} class="px-3 py-1 text-xs rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200 disabled:opacity-50">
				{pushing ? 'Updating…' : 'Update in HubSpot'}
			</button>
		{:else if !picking}
			<button onclick={openPicker} disabled={loading} class="px-3 py-1 text-xs rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200 disabled:opacity-50">
				Send to HubSpot
			</button>
		{/if}
	</div>
	{#if pushed && hs && !picking}
		<p class="text-[11px] text-gray-500">
			Sent {new Date(hs.pushed_at!).toLocaleString()} for {hs.contacts.map((c) => c.name).join(', ')}. Updating rewrites the
			same meeting, note and tasks.
		</p>
	{:else if !picking}
		<p class="text-[11px] text-gray-600">The meeting with its summary, a note and the action items as tasks, on the client's contact and company.</p>
	{/if}
	{#if picking}
		{#if loading}
			<p class="text-xs text-gray-500">Looking up contacts…</p>
		{:else if candidates}
			{#if candidates.length}
				<ul class="space-y-1" aria-label="HubSpot contacts">
					{#each candidates as c (c.id)}
						<li>
							<label class="flex items-baseline gap-2 text-sm text-gray-200">
								<input type="checkbox" checked={chosen.has(c.id)} onchange={() => toggle(c.id)} class="rounded border-gray-600 bg-gray-800" />
								<span>{c.name}</span>
								{#if c.email}<span class="text-xs text-gray-500">{c.email}</span>{/if}
								{#if c.company}<span class="text-xs text-gray-400">· {c.company}</span>{/if}
								<span class="ml-auto text-[11px] text-gray-600">
									{c.matched_by === 'confirmed' ? 'chosen before' : c.matched_by === 'email' ? 'by email' : `name “${c.matched_on}”`}
								</span>
							</label>
						</li>
					{/each}
				</ul>
			{:else}
				<p class="text-xs text-gray-500">
					No HubSpot contact found{searched.length ? ` for ${searched.join(', ')}` : ''}. Add the attendees' emails or name the speakers, then try again.
				</p>
			{/if}
			<div class="flex items-center gap-2">
				<button
					onclick={() => push([...chosen])}
					disabled={pushing || chosen.size === 0}
					class="px-3 py-1 text-xs rounded bg-orange-700 hover:bg-orange-600 text-white disabled:opacity-50"
				>
					{pushing ? 'Sending…' : pushed ? `Update with ${chosen.size} contact${chosen.size > 1 ? 's' : ''}` : `Send for ${chosen.size} contact${chosen.size === 1 ? '' : 's'}`}
				</button>
				<button onclick={() => (picking = false)} class="px-3 py-1 text-xs rounded text-gray-400 hover:text-gray-200">Cancel</button>
			</div>
		{/if}
	{/if}
</section>
