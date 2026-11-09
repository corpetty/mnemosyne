<script lang="ts" module>
	export type Role = 'main' | 'satellite' | 'team';
</script>

<script lang="ts">
	// Settings → Devices and people → This computer: what this computer is to the others.
	// The selected card is the current role (derived, not stored): a team server when team mode is
	// on, "uses my main computer" when this window is connected through remote access, else main.
	import LibraryCopy from './LibraryCopy.svelte';
	import RemoteConnect from './RemoteConnect.svelte';
	import TeamHostSettings from './TeamHostSettings.svelte';

	let { role, readOnly = false }: { role: Role; readOnly?: boolean } = $props();

	// The card the person is looking at; the current role until they pick another.
	let picked = $state<Role | null>(null);
	const pick = $derived(picked ?? role);

	const CARDS: { id: Role; title: string; text: string }[] = [
		{
			id: 'main',
			title: 'Main computer',
			text: 'Holds your library and does the work: recording, transcription, summaries.'
		},
		{
			id: 'satellite',
			title: 'Uses my main computer',
			text: "A laptop: shows your main computer's library and records into it."
		},
		{ id: 'team', title: 'Team server', text: 'Other people sign in to this computer.' }
	];
</script>

<section aria-label="This computer">
	<h3 class="text-lg font-semibold text-gray-200 mb-1">This computer</h3>
	<div class="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-3" role="radiogroup" aria-label="What this computer is">
		{#each CARDS as c (c.id)}
			<label
				class="relative block rounded border px-3 py-2 {readOnly ? 'opacity-60' : 'cursor-pointer'} {pick === c.id
					? 'border-blue-600 bg-blue-950/30'
					: 'border-gray-700 hover:border-gray-600'}"
			>
				<input
					type="radio"
					name="computer-role"
					value={c.id}
					checked={pick === c.id}
					disabled={readOnly}
					onchange={() => (picked = c.id)}
					class="sr-only"
				/>
				<span class="block text-sm font-medium {pick === c.id ? 'text-gray-100' : 'text-gray-300'}">{c.title}</span>
				<span class="block text-xs text-gray-500 mt-0.5">{c.text}</span>
			</label>
		{/each}
	</div>

	{#if pick === 'main'}
		{#if role === 'satellite'}
			<p class="text-xs text-gray-500">
				This window is connected to your main computer. Disconnect to use this computer on its own again.
			</p>
			<RemoteConnect />
		{:else if role === 'team'}
			<TeamHostSettings part="role" />
		{:else}
			<p class="text-xs text-gray-500">
				Your devices reach this computer while Mnemosyne runs. To keep it running with the window closed, turn on
				“Closing the window keeps Mnemosyne running in the tray” in General → Window.
			</p>
		{/if}
	{:else if pick === 'satellite'}
		<RemoteConnect />
		{#if role === 'satellite'}<LibraryCopy />{/if}
	{:else}
		{#if role !== 'team'}
			<p class="text-xs text-gray-500">
				Others sign in to this computer from a browser. Library sync and sharing with people who use Mnemosyne stop, and
				members cannot publish or open live-notes links until you allow it.
			</p>
		{/if}
		<TeamHostSettings part="role" />
	{/if}
</section>
