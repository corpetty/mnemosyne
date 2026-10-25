<script lang="ts">
	import { canChange } from '$lib/app/access.js';
	import { copyText } from '$lib/app/clipboard.js';
	import { openView } from '$lib/app/controller.svelte.js';
	import { guestNotesState } from '$lib/stores/guestNotes.svelte.js';
	import { peekNotes } from '$lib/stores/notes.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { uiState } from '$lib/stores/ui.svelte.js';

	// The owner's controls for guest notes: a link anyone in the meeting can type in the notes
	// through (backend guest notes room). `compact` is the small button beside the Copilot.
	let { compact = false }: { compact?: boolean } = $props();

	let open = $state(false);
	let root = $state<HTMLElement | null>(null);
	let confirming = $state<'end' | 'new' | 'stop' | null>(null);
	let showQr = $state(false);
	let qr = $state('');
	let noHost = $state(false);

	const session = $derived(sessionState.activeSession);
	const sid = $derived(session?.id ?? '');
	const visible = $derived(!!session && !session.shared_from && canChange(session) && guestNotesState.allowed);
	const loaded = $derived(guestNotesState.rooms[sid]);
	// "stopped" is the same as no room.
	const room = $derived(loaded && loaded.state !== 'stopped' ? loaded : null);
	const live = $derived(room?.state === 'live');

	$effect(() => {
		if (!guestNotesState.settingsLoaded) void guestNotesState.loadSettings();
	});
	$effect(() => {
		if (visible && sid) void guestNotesState.load(sid);
	});
	$effect(() => {
		void sid;
		open = false;
		confirming = null;
		showQr = false;
		noHost = false;
	});

	$effect(() => {
		const url = room?.url;
		if (!showQr || !url || !live) {
			qr = '';
			return;
		}
		let stale = false;
		import('qrcode')
			.then((QR) => QR.toDataURL(url, { margin: 1, width: 160, color: { dark: '#030712', light: '#f3f4f6' } }))
			.then((d) => {
				if (!stale) qr = d;
			})
			.catch(() => {
				if (!stale) qr = '';
			});
		return () => {
			stale = true;
		};
	});

	const when = (iso: string) =>
		iso ? new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : 'it expires';

	/** Who is here: the open notes document knows best (it is live), else the room's last count. */
	function presentNames(): string[] {
		const doc = sid ? peekNotes(sid) : undefined;
		return doc && doc.status === 'live' ? doc.guests : (room?.guests_present ?? []);
	}

	function toggleOpen() {
		open = !open;
		confirming = null;
		if (open && sid) void guestNotesState.load(sid);
	}

	function openSettings() {
		open = false;
		uiState.settingsTab = 'general';
		openView('settings');
	}

	/** Run a change; failures go to the toast (a missing host also shows the hint). */
	async function run(call: (id: string) => Promise<unknown>, done?: string) {
		noHost = false;
		confirming = null;
		await call(sid);
		const err = guestNotesState.error;
		if (err) {
			if (/^409: No host for guest notes/.test(err)) noHost = true;
			else toastState.error(err.replace(/^\d+: /, ''));
		} else if (done) {
			toastState.success(done);
		}
	}

	const create = () => run((id) => guestNotesState.start(id));
	const endRoom = () => run((id) => guestNotesState.end(id));
	const stopRoom = () => run((id) => guestNotesState.stop(id));
	const newLink = () => run((id) => guestNotesState.newLink(id), 'New link ready');

	const btnClass = $derived(
		compact
			? 'px-2 py-0.5 rounded border border-gray-700 text-xs'
			: 'px-2 py-0.5 rounded border border-gray-800'
	);
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
	<span class="relative inline-block" bind:this={root}>
		<button
			onclick={toggleOpen}
			aria-expanded={open}
			title="Let the people in this meeting take notes with you"
			class="{btnClass} {live ? 'text-green-400' : 'text-gray-400'} hover:text-gray-200"
		>{#if compact}{live ? 'Guests · live' : 'Invite guests to the notes'}{:else if live}Guests · live{:else if room}Guests · ended{:else}Invite guests{/if}</button>
		{#if open}
			<div
				class="absolute right-0 z-30 mt-1 w-80 rounded-lg border border-gray-700 bg-gray-900 p-3 text-left text-sm font-normal shadow-xl space-y-2"
				role="dialog"
				aria-label="Guest notes"
			>
				{#if !room}
					<p class="font-medium text-gray-200">Take notes with the people in this meeting</p>
					<p class="text-xs text-gray-400">
						Anyone with the link can type in these notes, comment on them and see the agenda. Nothing else from the meeting goes out.
					</p>
					{#if noHost}
						<p class="text-xs text-amber-300">Set a publish host in Settings to invite guests.</p>
						<button onclick={openSettings} class="rounded bg-gray-800 border border-gray-700 px-3 py-1 text-xs text-gray-200 hover:bg-gray-700">Open Settings</button>
					{/if}
					<button
						onclick={create}
						disabled={guestNotesState.busy}
						class="w-full rounded bg-blue-600 px-3 py-1 text-white hover:bg-blue-500 disabled:opacity-50"
					>{guestNotesState.busy ? 'Creating…' : 'Create a guest link'}</button>
				{:else}
					{#if live}
						<div class="flex items-center gap-2">
							<input
								readonly
								value={room.url ?? ''}
								aria-label="Guest link"
								onfocus={(e) => (e.currentTarget as HTMLInputElement).select()}
								class="min-w-0 flex-1 rounded border border-gray-700 bg-gray-950 px-2 py-1 font-mono text-xs text-gray-300"
							/>
							<button
								onclick={() => room.url && copyText(room.url, 'Link copied')}
								disabled={!room.url}
								class="shrink-0 rounded bg-gray-800 border border-gray-700 px-2 py-1 text-xs text-gray-200 hover:bg-gray-700 disabled:opacity-50"
							>Copy</button>
						</div>
						<button onclick={() => (showQr = !showQr)} class="text-xs text-blue-400 hover:text-blue-300">
							{showQr ? 'Hide QR code' : 'Show QR code'}
						</button>
						{#if showQr && qr}
							<img src={qr} alt="QR code for the guest link" class="h-40 w-40 rounded" />
						{/if}
						{@const names = presentNames()}
						<p class="text-xs text-gray-400">
							{names.length ? `Here now: ${names.join(', ')}` : 'Nobody has opened it yet'}
						</p>
					{:else}
						<p class="text-xs text-gray-300">Guest notes ended. Guests can read them until {when(room.expires_at)}.</p>
					{/if}

					{#if confirming}
						<div class="space-y-1 rounded border border-gray-800 bg-gray-950/40 p-2">
							<p class="text-xs text-gray-300">
								{#if confirming === 'end'}
									End guest notes? Guests can still read and copy the notes until {when(room.expires_at)}, but nobody can type.
								{:else if confirming === 'new'}
									Make a new link? The current one stops working.
								{:else}
									Delete the guest notes now? The link stops working for everyone.
								{/if}
							</p>
							<div class="flex gap-3 text-xs">
								<button
									onclick={confirming === 'end' ? endRoom : confirming === 'new' ? newLink : stopRoom}
									disabled={guestNotesState.busy}
									class="{confirming === 'stop' ? 'text-red-400 hover:text-red-300' : 'text-blue-400 hover:text-blue-300'} disabled:opacity-50"
								>Yes</button>
								<button onclick={() => (confirming = null)} disabled={guestNotesState.busy} class="text-gray-400 hover:text-gray-200">No</button>
							</div>
						</div>
					{:else}
						<div class="flex flex-wrap items-center gap-2 text-xs">
							{#if live}
								<button
									onclick={() => (confirming = 'end')}
									disabled={guestNotesState.busy}
									class="rounded bg-gray-800 border border-gray-700 px-2 py-1 text-gray-200 hover:bg-gray-700 disabled:opacity-50"
								>End guest notes</button>
							{/if}
							<button
								onclick={() => (confirming = 'new')}
								disabled={guestNotesState.busy}
								class="rounded bg-gray-800 border border-gray-700 px-2 py-1 text-gray-200 hover:bg-gray-700 disabled:opacity-50"
							>New link</button>
							<button
								onclick={() => (confirming = 'stop')}
								disabled={guestNotesState.busy}
								class="rounded border border-red-900 px-2 py-1 text-red-400 hover:text-red-300 disabled:opacity-50"
							>Stop sharing the notes</button>
						</div>
					{/if}
				{/if}
			</div>
		{/if}
	</span>
{/if}
