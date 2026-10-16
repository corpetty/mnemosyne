<script lang="ts">
	import { audioUrl } from '$lib/api/backend.js';
	import { playerState } from '$lib/stores/player.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { syncState } from '$lib/stores/sync.svelte.js';
	import { audioState } from '$lib/stores/audio.svelte.js';

	let el = $state<HTMLAudioElement>();
	let sourceId = $state<string>('mixed');
	// Audio that cannot be had here (a meeting recorded on another computer that is not reachable).
	let unavailable = $state('');

	const session = $derived(sessionState.activeSession);
	const src = $derived(
		(session?.audio_file || session?.origin) && !audioState.isRecording
			? audioUrl(session.id, sourceId === 'mixed' ? undefined : sourceId)
			: null
	);

	$effect(() => {
		playerState.attach(el ?? null);
		return () => playerState.attach(null);
	});

	// Reset only when a different session is shown; edits replace the session
	// object without changing its id and must not disturb playback.
	let lastSessionId: string | null = null;
	$effect(() => {
		const id = session?.id ?? null;
		if (id !== lastSessionId) {
			lastSessionId = id;
			sourceId = 'mixed';
			playerState.reset();
			unavailable = '';
		}
	});

	// "Available" means the meeting has audio; the element itself is created lazily.
	$effect(() => {
		playerState.available = !!src;
	});

	// The element does not say why it failed; ask the same URL for the reason (409: the audio is
	// on the computer that recorded it).
	async function checkError() {
		playerState.available = false;
		if (!session?.origin || !src) return;
		try {
			const r = await fetch(src, { headers: { Range: 'bytes=0-0' } });
			if (r.status === 409) {
				let detail = '';
				try {
					detail = (await r.json()).detail ?? '';
				} catch {
					/* no body */
				}
				unavailable = detail || syncState.audioOn(session.origin);
			}
		} catch {
			/* offline: stays silent */
		}
	}

	function fmt(t: number): string {
		if (!isFinite(t)) return '0:00';
		const m = Math.floor(t / 60);
		const s = Math.floor(t % 60);
		return `${m}:${String(s).padStart(2, '0')}`;
	}
</script>

{#if unavailable}
	<p class="text-xs text-gray-500">{unavailable}</p>
{:else if src}
	<div class="flex items-center gap-3 rounded-lg border border-gray-800 bg-gray-900/60 px-3 py-2">
		<button
			onclick={() => playerState.toggle()}
			class="w-8 h-8 rounded-full bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200 flex items-center justify-center text-sm"
			title="Play / pause (click a timestamp in the transcript to jump)"
		>
			{playerState.playing ? '❚❚' : '▶'}
		</button>
		{#if playerState.wanted}
			<audio
				bind:this={el}
				{src}
				preload="metadata"
				onplay={() => (playerState.playing = true)}
				onpause={() => (playerState.playing = false)}
				ontimeupdate={() => playerState.timeUpdate(el?.currentTime ?? 0)}
				onloadedmetadata={() => {
					playerState.duration = el?.duration ?? 0;
					playerState.ready();
				}}
				onerror={checkError}
				class="hidden"
			></audio>
		{/if}
		<input
			type="range"
			min="0"
			max={playerState.duration || 0}
			step="0.1"
			value={playerState.currentTime}
			oninput={(e) => playerState.seek(Number((e.currentTarget as HTMLInputElement).value), false)}
			class="flex-1 accent-blue-500"
		/>
		<span class="font-mono text-xs text-gray-400 w-24 text-right">{fmt(playerState.currentTime)} / {fmt(playerState.duration)}</span>
		{#if (session?.recordings.length ?? 0) > 1}
			<select bind:value={sourceId} class="bg-gray-800 border border-gray-700 rounded px-1.5 py-1 text-xs text-gray-300" title="Which audio to play">
				<option value="mixed">Mixed</option>
				{#each session?.recordings ?? [] as r (r.id)}
					<option value={r.id}>{r.source}: {r.device_name}</option>
				{/each}
			</select>
		{/if}
	</div>
{/if}
