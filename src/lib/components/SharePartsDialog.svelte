<script lang="ts">
	import { onMount } from 'svelte';
	import { contactsState } from '$lib/stores/contacts.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { PeerShareInfo, SessionDetail, ShareParts } from '$lib/types/index.js';
	import PartPicker from './PartPicker.svelte';

	// Share a meeting with one person from the contacts (backend services/share_hub.py): which parts
	// travel, and stopping the share. `existing` is the live share when this changes one.
	let {
		session,
		person,
		existing,
		onclose
	}: { session: SessionDetail; person: string; existing: PeerShareInfo | null; onclose: () => void } = $props();

	const name = $derived(contactsState.name(person));
	const data = $derived(session.summary_data);

	const FRESH: ShareParts = {
		summary: true,
		decisions: true,
		questions: true,
		action_items: true,
		chapters: true,
		topics: true,
		attendees: true,
		notes: true,
		transcript: false,
		hide_identifiers: false
	};
	// Seeded once from the props: the dialog is opened per share.
	// svelte-ignore state_referenced_locally
	const start: ShareParts = { ...(existing?.parts ?? FRESH) };
	let parts = $state<ShareParts>({ ...start });
	let busy = $state(false);
	let confirmStop = $state(false);
	let panel = $state<HTMLElement | null>(null);

	const items = $derived([
		{ key: 'summary', label: 'Summary' },
		{ key: 'decisions', label: 'Decisions', count: data?.decisions.length ?? 0 },
		{ key: 'questions', label: 'Open questions', count: data?.open_questions.length ?? 0 },
		{ key: 'action_items', label: 'Action items', count: data?.action_items.length ?? 0 },
		{ key: 'chapters', label: 'Chapters' },
		{ key: 'topics', label: 'Topics' },
		{ key: 'attendees', label: 'Attendees' },
		{ key: 'notes', label: 'Notes' }
	]);
	const others = $derived(
		(contactsState.peerShares[session.id] ?? []).filter((s) => s.person !== person).map((s) => contactsState.name(s.person))
	);
	const changed = $derived(!existing || (Object.keys(start) as (keyof ShareParts)[]).some((k) => parts[k] !== start[k]));

	onMount(() => panel?.querySelector<HTMLElement>('input, button')?.focus());

	function close() {
		if (!busy) onclose();
	}

	async function save() {
		busy = true;
		try {
			await contactsState.share(session.id, person, { ...parts });
			toastState.success(existing ? `Sharing with ${name} saved` : `Shared with ${name}`);
			onclose();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not share');
		} finally {
			busy = false;
		}
	}

	async function stop() {
		busy = true;
		try {
			await contactsState.unshare(session.id, person);
			toastState.success(`Stopped sharing with ${name}`);
			onclose();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not stop sharing');
		} finally {
			busy = false;
		}
	}

	const check = 'rounded border-gray-600 bg-gray-800';
</script>

<div class="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-black/50 px-4 py-[5vh]" role="presentation" onclick={close}>
	<div
		bind:this={panel}
		class="w-full max-w-md space-y-4 rounded-lg border border-gray-700 bg-gray-900 p-4 shadow-2xl"
		role="dialog"
		aria-modal="true"
		aria-label="Share this meeting with {name}"
		tabindex="-1"
		onclick={(e) => e.stopPropagation()}
		onkeydown={(e) => e.key === 'Escape' && close()}
	>
		<h3 class="text-lg font-semibold text-gray-100">Share “{session.name}” with {name}</h3>
		<p class="text-sm text-gray-400">{name} can comment, tick action items and type in the notes you share. Only you change the rest.</p>

		<div class="space-y-4 text-sm">
			<PartPicker
				{items}
				sel={parts as unknown as Record<string, boolean>}
				onchange={(key, value) => (parts = { ...parts, [key]: value })}
			/>
			<p class="-mt-2 text-xs text-gray-500">The notes go with their whole edit history.</p>

			<div>
				<label class="flex items-center gap-2 text-gray-300">
					<input type="checkbox" class={check} checked={parts.transcript} onchange={(e) => (parts = { ...parts, transcript: e.currentTarget.checked })} />
					Transcript
				</label>
				{#if parts.transcript}
					<p class="mt-1 text-xs text-amber-300/80">The whole transcript goes to {name}'s computer and can't be taken back.</p>
				{/if}
			</div>

			<div>
				<label class="flex items-center gap-2 text-gray-300">
					<input
						type="checkbox"
						class={check}
						checked={parts.hide_identifiers}
						onchange={(e) => (parts = { ...parts, hide_identifiers: e.currentTarget.checked })}
					/>
					Hide emails, phone numbers and account numbers
				</label>
				<p class="mt-1 text-xs text-gray-500">Covers the summary and transcript, not the notes or comments.</p>
			</div>

			{#if others.length}<p class="text-xs text-gray-500">Also shared with {others.join(', ')}.</p>{/if}
		</div>

		<div class="flex flex-wrap items-center justify-end gap-3 border-t border-gray-800 pt-3">
			{#if existing}
				{#if confirmStop}
					<p class="mr-auto basis-full text-xs text-gray-300">
						Stop sharing with {name}? Their copy is removed when their computer next checks in. What they already saw can't be recalled.
					</p>
					<button onclick={stop} disabled={busy} class="mr-auto text-sm text-red-400 hover:text-red-300 disabled:opacity-50">Yes, stop sharing</button>
					<button onclick={() => (confirmStop = false)} disabled={busy} class="text-sm text-gray-400 hover:text-gray-200">No</button>
				{:else}
					<button onclick={() => (confirmStop = true)} disabled={busy} class="mr-auto text-sm text-red-400 hover:text-red-300 disabled:opacity-50">Stop sharing</button>
				{/if}
			{/if}
			{#if !confirmStop}
				<button onclick={close} disabled={busy} class="px-3 py-1.5 text-sm text-gray-400 hover:text-gray-200 disabled:opacity-50">Cancel</button>
				<button
					onclick={save}
					disabled={busy || !changed}
					class="rounded bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-60"
				>
					{busy ? 'Saving…' : existing ? 'Save' : 'Share'}
				</button>
			{/if}
		</div>
	</div>
</div>
