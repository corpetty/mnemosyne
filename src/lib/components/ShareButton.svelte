<script lang="ts">
	import { canChange } from '$lib/app/access.js';
	import { guestNotesState } from '$lib/stores/guestNotes.svelte.js';
	import { publishingState } from '$lib/stores/publishing.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import GuestNotesMenu from './GuestNotesMenu.svelte';
	import PeopleShareMenu from './PeopleShareMenu.svelte';
	import ShareMenu from './ShareMenu.svelte';

	// The one Share button of the meeting header. Each way keeps its logic in its own component, mounted
	// here embedded: it says whether it applies (`available`) and draws its panel when it is chosen.
	type Way = 'people' | 'live' | 'team';
	let open = $state(false);
	let way = $state<Way | null>(null);
	let root = $state<HTMLElement | null>(null);
	let peopleOk = $state(false);
	let liveOk = $state(false);
	let teamOk = $state(false);

	const session = $derived(sessionState.activeSession);
	const sid = $derived(session?.id ?? '');
	const canPublish = $derived(!!session && canChange(session) && publishingState.allowed);
	const liveNow = $derived(guestNotesState.rooms[sid]?.state === 'live');
	const any = $derived(peopleOk || liveOk || teamOk || canPublish);

	$effect(() => {
		void sid;
		open = false;
		way = null;
	});

	function toggle() {
		open = !open;
		way = null;
	}
	function close() {
		open = false;
		way = null;
	}
	function publish() {
		close();
		if (session) publishingState.open(session);
	}

	const item = 'block w-full px-3 py-1.5 text-left text-gray-200 hover:bg-gray-800';
</script>

<svelte:window
	onclick={(e) => {
		if (open && root && !e.composedPath().includes(root)) close();
	}}
	onkeydown={(e) => {
		if (open && e.key === 'Escape') close();
	}}
/>

<span class="relative" bind:this={root}>
	{#if any}
		<button
			onclick={toggle}
			aria-expanded={open}
			aria-haspopup="menu"
			title="Share this meeting"
			class="px-2 py-0.5 rounded border border-gray-800 {liveNow ? 'text-green-400' : 'text-gray-400'} hover:text-gray-200"
		>Share</button>
	{/if}
	{#if open && !way}
		<div class="absolute right-0 z-30 mt-1 w-72 rounded-lg border border-gray-700 bg-gray-900 py-1 text-sm font-normal shadow-xl" role="menu">
			{#if peopleOk}
				<button role="menuitem" class={item} onclick={() => (way = 'people')}>
					People with Mnemosyne…
					<span class="block text-xs text-gray-500">A copy of the parts you pick, kept up to date</span>
				</button>
			{/if}
			{#if liveOk}
				<button role="menuitem" class={item} onclick={() => (way = 'live')}>
					A link to the live notes…
					<span class="block text-xs text-gray-500">For the people in this meeting, while it runs</span>
				</button>
			{/if}
			{#if canPublish}
				<button role="menuitem" class={item} onclick={publish}>
					A page…
					<span class="block text-xs text-gray-500">A curated page anyone with the link can read</span>
				</button>
			{/if}
			{#if teamOk}
				<button role="menuitem" class={item} onclick={() => (way = 'team')}>
					Team members…
					<span class="block text-xs text-gray-500">Accounts on this server</span>
				</button>
			{/if}
		</div>
	{/if}
	<!-- Always mounted: a choice keeps its state and its own dialogs (the parts dialog outlives the menu). -->
	<div class={open && way ? 'absolute right-0 z-30 mt-1 w-80 rounded-lg border border-gray-700 bg-gray-900 p-3 shadow-xl space-y-2' : ''}>
		{#if open && way}
			<button onclick={() => (way = null)} class="text-xs text-gray-400 hover:text-gray-200">‹ Share</button>
		{/if}
		<PeopleShareMenu embedded show={open && way === 'people'} bind:available={peopleOk} onclose={close} />
		<GuestNotesMenu embedded show={open && way === 'live'} bind:available={liveOk} onclose={close} />
		<ShareMenu embedded show={open && way === 'team'} bind:available={teamOk} onclose={close} />
	</div>
</span>
