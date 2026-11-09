<script lang="ts" module>
	export type Role = 'main' | 'satellite' | 'team';
</script>

<script lang="ts">
	// Settings → Devices and people → This computer: what this computer is to the others.
	// The selected card is the current role (derived, not stored): a team server when this is a
	// dedicated team server, "uses my main computer" when this window is connected through remote
	// access, else main. A main computer can also host a team ("Host a team on this computer").
	import { onExternalClick } from '$lib/app/externalLink.js';
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
		{ id: 'team', title: 'Team server', text: 'A computer that belongs to a team, not to one person.' }
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
			<div class="mt-4">
				<h4 class="text-sm font-medium text-gray-300">Host a team on this computer</h4>
				<p class="text-xs text-gray-500">
					Teammates sign in to this computer from a browser and record there. Your library, your devices and sharing with
					people keep working; teammates' meetings never go to your other computers, your vault or the people you share
					with.
				</p>
				<TeamHostSettings part="role" />
			</div>
		{/if}
	{:else if pick === 'satellite'}
		<RemoteConnect />
		{#if role === 'satellite'}<LibraryCopy />{/if}
	{:else if role === 'team'}
		<TeamHostSettings part="role" />
	{:else}
		<p class="text-xs text-gray-500">
			On a team server, library sync, laptops and sharing with people are off. It is set up on its own machine (see the
			<a
				href="https://github.com/corpetty/mnemosyne/blob/main/docs/team-server.md"
				onclick={onExternalClick}
				target="_blank"
				rel="noopener noreferrer"
				class="underline hover:text-gray-300">team server guide</a
			>). To let teammates use this computer while it stays yours, choose Main computer → Host a team on this computer.
		</p>
	{/if}
</section>
