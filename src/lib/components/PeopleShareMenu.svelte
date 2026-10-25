<script lang="ts">
	import { openView } from '$lib/app/controller.svelte.js';
	import { connectionState } from '$lib/stores/connection.svelte.js';
	import { contactsState } from '$lib/stores/contacts.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { uiState } from '$lib/stores/ui.svelte.js';
	import type { PeerShareInfo, ShareParts } from '$lib/types/index.js';
	import SharePartsDialog from './SharePartsDialog.svelte';

	// Share this meeting with people from the contacts, desktop only (backend services/share_hub.py).
	// The team-server counterpart is ShareMenu.
	let open = $state(false);
	let root = $state<HTMLElement | null>(null);
	let dialog = $state<{ person: string; existing: PeerShareInfo | null } | null>(null);
	let confirming = $state<string | null>(null);
	let busy = $state(false);

	const session = $derived(sessionState.activeSession);
	const sid = $derived(session?.id ?? '');
	const visible = $derived(
		!!session && !connectionState.me?.team_mode && contactsState.available && !session.shared_from
	);
	const shares = $derived(contactsState.peerShares[sid] ?? []);
	const label = $derived(shares.length ? `Shared · ${shares.length}` : 'Share');

	$effect(() => {
		if (!connectionState.me?.team_mode && !contactsState.loaded) void contactsState.load();
	});
	$effect(() => {
		if (visible && sid) void contactsState.loadShares(sid);
	});

	function shareFor(person: string): PeerShareInfo | null {
		return shares.find((s) => s.person === person) ?? null;
	}

	const PART_LABELS: [keyof ShareParts, string][] = [
		['summary', 'summary'],
		['decisions', 'decisions'],
		['questions', 'open questions'],
		['action_items', 'action items'],
		['chapters', 'chapters'],
		['topics', 'topics'],
		['attendees', 'attendees'],
		['notes', 'notes']
	];
	/** "Summary, decisions, action items, notes": only the ticked parts. */
	function partsLine(parts: ShareParts): string {
		const text = PART_LABELS.filter(([k]) => parts[k]).map(([, l]) => l).join(', ');
		return text ? text[0].toUpperCase() + text.slice(1) : '';
	}

	function toggleOpen() {
		open = !open;
		confirming = null;
		if (open && sid) void contactsState.loadShares(sid);
	}

	function openDialog(person: string, existing: PeerShareInfo | null) {
		open = false;
		confirming = null;
		dialog = { person, existing };
	}

	function addPerson() {
		open = false;
		uiState.settingsTab = 'general';
		openView('settings');
	}

	async function stop(person: string) {
		busy = true;
		try {
			await contactsState.unshare(sid, person);
			toastState.success(`Stopped sharing with ${contactsState.name(person)}`);
			confirming = null;
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not stop sharing');
		} finally {
			busy = false;
		}
	}
</script>

<svelte:window
	onclick={(e) => {
		if (open && root && !e.composedPath().includes(root)) open = false;
	}}
	onkeydown={(e) => {
		if (open && e.key === 'Escape') open = false;
	}}
/>

{#if visible}
	<span class="relative" bind:this={root}>
		<button
			onclick={toggleOpen}
			aria-expanded={open}
			title="Share this meeting with people"
			class="px-2 py-0.5 rounded border border-gray-800 {shares.length ? 'text-blue-300' : 'text-gray-400'} hover:text-gray-200"
		>{label}</button>
		{#if open}
			<div class="absolute right-0 z-30 mt-1 w-72 rounded-lg border border-gray-700 bg-gray-900 p-3 text-sm shadow-xl space-y-2" role="dialog" aria-label="Share this meeting with people">
				<p class="text-xs text-gray-500">People you share with</p>
				{#if contactsState.contacts.length}
					<ul class="max-h-64 space-y-2 overflow-y-auto">
						{#each contactsState.contacts as c (c.person)}
							{@const s = shareFor(c.person)}
							<li>
								<div class="flex items-center justify-between gap-2">
									<span class="min-w-0 truncate text-gray-200">{c.name || c.card_name}</span>
									{#if !s}
										<button onclick={() => openDialog(c.person, null)} class="shrink-0 text-xs text-blue-400 hover:text-blue-300">Share…</button>
									{:else}
										<span class="flex shrink-0 gap-2 text-xs">
											<button onclick={() => openDialog(c.person, s)} class="text-blue-400 hover:text-blue-300">Change</button>
											<button onclick={() => (confirming = c.person)} class="text-red-400 hover:text-red-300">Stop</button>
										</span>
									{/if}
								</div>
								{#if s}
									<p class="truncate text-xs text-gray-500">
										{partsLine(s.parts)}{#if s.parts.transcript}{#if partsLine(s.parts)}, {/if}<span class="text-amber-300/80">transcript</span>{/if}
									</p>
								{/if}
								{#if confirming === c.person}
									<div class="mt-1 space-y-1 rounded border border-gray-800 bg-gray-950/40 p-2">
										<p class="text-xs text-gray-300">
											Stop sharing with {c.name || c.card_name}? Their copy is removed when their computer next checks in. What they already saw can't be recalled.
										</p>
										<div class="flex gap-3 text-xs">
											<button onclick={() => stop(c.person)} disabled={busy} class="text-red-400 hover:text-red-300 disabled:opacity-50">Yes, stop sharing</button>
											<button onclick={() => (confirming = null)} disabled={busy} class="text-gray-400 hover:text-gray-200">No</button>
										</div>
									</div>
								{/if}
							</li>
						{/each}
					</ul>
					<button onclick={addPerson} class="text-xs text-gray-400 hover:text-gray-200">Add a person…</button>
				{:else}
					<p class="text-gray-400">Nobody to share with yet.</p>
					<button onclick={addPerson} class="w-full rounded bg-blue-600 px-3 py-1 text-white hover:bg-blue-500">Add a person</button>
				{/if}
			</div>
		{/if}
	</span>
{/if}

{#if dialog && session}
	<SharePartsDialog {session} person={dialog.person} existing={dialog.existing} onclose={() => (dialog = null)} />
{/if}
