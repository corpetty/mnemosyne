<script lang="ts">
	// Settings → Connection: people whose computers this one shares chosen meetings with
	// (backend services/contacts.py, share_pull.py).
	import { createContactCode, pairContact, pullContact, removeContact, renameContact } from '$lib/api/backend.js';
	import { ago } from '$lib/app/syncText.js';
	import { contactsState } from '$lib/stores/contacts.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { ContactInfo, PairingCode } from '$lib/types/index.js';

	let { enabled = false, tokenSet = false }: { enabled?: boolean; tokenSet?: boolean } = $props();

	let code = $state<PairingCode | null>(null);
	let copied = $state(false);
	let invite = $state('');
	let joining = $state(false);
	let paired = $state<ContactInfo | null>(null);
	let editing = $state<string | null>(null);
	let draft = $state('');
	let now = $state(Date.now());

	const ready = $derived(enabled && tokenSet);

	$effect(() => {
		if (ready) void contactsState.load();
	});
	$effect(() => {
		const t = setInterval(() => (now = Date.now()), 30_000);
		return () => clearInterval(t);
	});

	const fail = (e: unknown, fallback: string) => toastState.error(e instanceof Error ? e.message : fallback);
	const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

	async function makeInvite() {
		copied = false;
		try {
			code = await createContactCode();
		} catch (e) {
			fail(e, 'Could not create an invite');
		}
	}

	async function copy() {
		if (!code?.invite) return;
		await navigator.clipboard.writeText(code.invite);
		copied = true;
	}

	async function join() {
		if (!invite.trim() || joining) return;
		joining = true;
		try {
			paired = await pairContact(invite.trim());
			invite = '';
			await contactsState.load();
		} catch (e) {
			fail(e, 'Could not pair');
		} finally {
			joining = false;
		}
	}

	function startRename(c: ContactInfo) {
		editing = c.person;
		draft = c.name === c.card_name ? '' : c.name;
	}

	async function saveRename(c: ContactInfo) {
		if (editing !== c.person) return;
		editing = null;
		const name = draft.trim();
		if (name === (c.name === c.card_name ? '' : c.name)) return;
		try {
			await renameContact(c.person, name);
			await contactsState.load();
		} catch (e) {
			fail(e, 'Could not rename');
		}
	}

	async function check(c: ContactInfo) {
		try {
			await pullContact(c.person);
			await contactsState.load();
		} catch (e) {
			fail(e, 'Could not check');
		}
	}

	async function remove(c: ContactInfo) {
		if (
			!confirm(
				`Remove ${c.name}? You stop sharing with them, and what they shared with you is removed from this computer. Copies they already have can't be recalled.`
			)
		)
			return;
		try {
			await removeContact(c.person);
			if (paired?.person === c.person) paired = null;
			await contactsState.load();
		} catch (e) {
			fail(e, 'Could not remove');
		}
	}

	function pullLine(c: ContactInfo): { text: string; cls: string } | null {
		const p = c.pull;
		if (!p) return null;
		switch (p.state) {
			case 'ok':
				return { text: `Up to date · ${ago(p.at, now)}`, cls: 'text-gray-500' };
			case 'pulling':
				return { text: 'Checking…', cls: 'text-gray-500' };
			case 'unreachable':
				return { text: "Their computer isn't reachable right now", cls: 'text-gray-500' };
			case 'skew':
				return { text: p.error ?? 'Versions differ', cls: 'text-amber-300' };
			case 'gone':
				return { text: `${c.name} no longer shares with you`, cls: 'text-amber-300' };
			case 'error':
				return { text: p.error ?? 'Something went wrong', cls: 'text-red-400' };
			default:
				return null;
		}
	}
</script>

{#if contactsState.available}
	<div class="mt-4 space-y-2" aria-label="People you share with">
		<h4 class="text-sm font-medium text-gray-300">People you share with</h4>
		<p class="text-xs text-gray-500">
			Pair with someone else's computer to share chosen meetings with them. They can comment, tick action items and type in the notes you share; the rest stays yours.
		</p>
		{#if !ready}
			<p class="text-xs text-amber-300">Needs remote access and an API token (above).</p>
		{/if}

		{#if contactsState.me}
			<div class="max-w-2xl space-y-1">
				<p class="text-xs text-gray-400">You: <span class="text-gray-200">{contactsState.me.name}</span></p>
				<p class="rounded bg-gray-800/60 px-2 py-1.5 font-mono text-xs text-gray-400 break-words">{contactsState.me.fingerprint}</p>
				<p class="text-xs text-gray-600">Read this to the person you pair with: they see the same on their screen.</p>
			</div>
		{/if}

		<div class="flex items-center gap-3">
			<button
				onclick={makeInvite}
				disabled={!ready}
				class="px-3 py-1.5 text-xs rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200 disabled:opacity-50"
				>Show my invite</button
			>
		</div>
		{#if code?.invite}
			<div class="flex items-center gap-2 max-w-2xl">
				<input
					readonly
					value={code.invite}
					aria-label="Your invite"
					class="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-xs text-gray-300 w-full font-mono"
				/>
				<button onclick={copy} class="text-xs text-gray-400 hover:text-gray-200 whitespace-nowrap">{copied ? 'Copied' : 'Copy'}</button>
			</div>
			<p class="text-xs text-gray-600">Send this to them; it works once, until {clock(code.expires_at)}.</p>
		{/if}

		<div class="flex items-center gap-2 max-w-2xl">
			<input
				type="text"
				bind:value={invite}
				placeholder="Paste their invite"
				aria-label="Their invite"
				disabled={!ready}
				onkeydown={(e) => e.key === 'Enter' && join()}
				class="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-xs text-gray-300 w-full font-mono disabled:opacity-50"
			/>
			<button
				onclick={join}
				disabled={!ready || !invite.trim() || joining}
				class="px-3 py-1.5 text-xs rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200 disabled:opacity-50"
				>{joining ? 'Pairing…' : 'Pair'}</button
			>
		</div>

		{#if paired && contactsState.me}
			<div class="max-w-2xl flex items-start gap-3 rounded border border-blue-800 bg-blue-950/40 px-3 py-2 text-xs text-blue-200">
				<p class="flex-1">
					Paired with {paired.name}. Compare fingerprints out loud: theirs is
					<span class="font-mono">{paired.fingerprint}</span>, yours is
					<span class="font-mono">{contactsState.me.fingerprint}</span>. If they differ, remove them.
				</p>
				<button onclick={() => (paired = null)} class="text-blue-300 hover:text-white whitespace-nowrap">Done</button>
			</div>
		{/if}

		{#if contactsState.contacts.length}
			<ul class="max-w-2xl divide-y divide-gray-800 rounded border border-gray-800">
				{#each contactsState.contacts as c (c.person)}
					{@const line = ready ? pullLine(c) : null}
					<li class="px-2 py-1.5 text-xs space-y-0.5">
						<div class="flex items-center gap-2">
							{#if editing === c.person}
								<!-- svelte-ignore a11y_autofocus -->
								<input
									type="text"
									bind:value={draft}
									aria-label="Name for {c.name}"
									placeholder={c.card_name}
									autofocus
									onkeydown={(e) => {
										if (e.key === 'Enter') void saveRename(c);
										else if (e.key === 'Escape') editing = null;
									}}
									onblur={() => saveRename(c)}
									class="bg-gray-800 border border-gray-700 rounded px-2 py-0.5 text-xs text-gray-200"
								/>
							{:else}
								<span class="text-gray-200">{c.name}</span>
								{#if c.name !== c.card_name}<span class="text-gray-500">({c.card_name})</span>{/if}
								<button
									onclick={() => startRename(c)}
									aria-label="Rename {c.name}"
									title="Rename"
									class="text-gray-500 hover:text-gray-200">✎</button
								>
							{/if}
							<span class="min-w-0 flex-1"></span>
							<button
								onclick={() => check(c)}
								disabled={c.pull?.state === 'pulling'}
								class="text-gray-400 hover:text-gray-200 disabled:opacity-50 whitespace-nowrap">Check now</button
							>
							<button onclick={() => remove(c)} class="text-gray-500 hover:text-red-400">Remove</button>
						</div>
						<div class="flex flex-wrap items-center gap-x-3 text-gray-500">
							<span class="font-mono text-gray-600 break-words">{c.fingerprint}</span>
							<span title={c.machines.join(', ')}>{c.machines.length} computer{c.machines.length === 1 ? '' : 's'}</span>
						</div>
						{#if line}<p class={line.cls}>{line.text}</p>{/if}
					</li>
				{/each}
			</ul>
		{:else if contactsState.loaded}
			<p class="text-xs text-gray-500">Nobody yet.</p>
		{/if}
	</div>
{/if}
