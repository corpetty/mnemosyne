<script lang="ts">
	import { backendMediaUrl } from '$lib/api/backend.js';
	import { copyText } from '$lib/app/clipboard.js';
	import {
		clock,
		expiryChoices,
		expiryLabel,
		joinedSpan,
		passwordProblem,
		previewQuoteFor,
		quoteExcerpt,
		quoteKey,
		shortDate,
		type PreviewQuote,
		type Target
	} from '$lib/app/publishing.js';
	import { publishingState as pub } from '$lib/stores/publishing.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { PublicationSelection, QuoteSpan } from '$lib/types/index.js';
	import PartPicker from './PartPicker.svelte';
	import Meeting from '../../../viewer/src/Meeting.svelte';
	import type { Manifest } from '../../../viewer/src/format';

	// Publish a meeting (backend services/publishing.py): the choices on the left, the viewer's
	// own rendering of the result on the right, refreshed as they change.
	const session = $derived(sessionState.activeSession?.id === pub.dialogFor ? sessionState.activeSession : null);
	const sel = $derived(pub.selection);
	const current = $derived(pub.current);
	const data = $derived(session?.summary_data ?? null);
	const manifest = $derived(pub.preview as Manifest | null);
	const previewQuotes = $derived(((pub.preview as { quotes?: PreviewQuote[] } | null)?.quotes ?? []) as PreviewQuote[]);
	const passwordIssue = $derived(pub.target === 'file' ? passwordProblem(pub.password) : '');
	const working = $derived(!!pub.busy);

	let confirmUnpublish = $state(false);
	/** The quote whose clip is being listened to (its key). */
	let listening = $state<string | null>(null);

	const content = $derived<{ key: keyof PublicationSelection; label: string; count?: number }[]>([
		{ key: 'summary', label: 'Summary' },
		{ key: 'decisions', label: 'Decisions', count: data?.decisions.length ?? 0 },
		{ key: 'questions', label: 'Open questions', count: data?.open_questions.length ?? 0 },
		{ key: 'action_items', label: 'Action items', count: data?.action_items.length ?? 0 },
		{ key: 'chapters', label: 'Chapters' },
		{ key: 'topics', label: 'Topics' },
		{ key: 'notes', label: 'Live notes' }
	]);
	const NAMES = [
		['as_is', 'Names'],
		['first', 'First names'],
		['hidden', 'Person 1']
	] as const;
	const targets = $derived<[Target, string][]>([
		['local', 'This computer'],
		['file', 'A file'],
		...(pub.publishHost ? [['host', 'Publish host'] as [Target, string]] : [])
	]);

	function close() {
		if (working) return;
		confirmUnpublish = false;
		pub.close();
	}

	/** Open a link in the system browser (Tauri's shell plugin), or a new tab in a browser. */
	async function openLink(href: string) {
		try {
			const { invoke } = await import('@tauri-apps/api/core');
			await invoke('plugin:shell|open', { path: href });
		} catch {
			window.open(href, '_blank', 'noopener,noreferrer');
		}
	}

	/** The preview renders clips from the list on the left, which plays this backend's audio. */
	async function previewClip(): Promise<Uint8Array> {
		throw new Error('Listen to it under Quotes, on the left');
	}

	async function primary() {
		confirmUnpublish = false;
		if (pub.target === 'file') {
			if (passwordIssue) return;
			if (await pub.saveFile()) toastState.success('Saved the meeting as a file');
			return;
		}
		const updating = !!current;
		const done = await pub.publish();
		if (done && updating) toastState.success(`Updated: version ${done.version}`);
	}

	async function unpublish() {
		if (await pub.unpublish()) {
			close();
			toastState.success('Unpublished. The link no longer works.');
		}
	}

	function quoteText(q: QuoteSpan): string {
		return previewQuoteFor(session, q, previewQuotes)?.text ?? quoteExcerpt(session, q);
	}

	function clipUrl(q: QuoteSpan): string | null {
		const url = previewQuoteFor(session, q, previewQuotes)?.clip?.preview_url;
		return url ? backendMediaUrl(url) : null;
	}

	const expiresLine = $derived(
		current?.expires_at ? `Anyone with this link can read it until ${shortDate(current.expires_at)}.` : 'Anyone with this link can read it.'
	);
	const primaryLabel = $derived(
		pub.target === 'file'
			? pub.busy === 'file'
				? 'Making the file…'
				: 'Save file'
			: pub.busy === 'publish'
				? current
					? 'Updating…'
					: 'Publishing…'
				: current
					? 'Update'
					: 'Publish'
	);

	const groupHeading = 'mb-1.5 text-[11px] uppercase tracking-wide text-gray-500';
	const check = 'rounded border-gray-600 bg-gray-800';
	const segmented = 'flex w-fit rounded border border-gray-700 overflow-hidden text-xs';
	const segment = (on: boolean) => `px-2.5 py-1 ${on ? 'bg-gray-700 text-gray-100' : 'text-gray-400 hover:bg-gray-800'}`;
	const inputClass = 'w-full rounded border border-gray-700 bg-gray-800 px-2 py-1.5 text-sm text-gray-200';
</script>

<div class="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-black/50 px-4 py-[5vh]" role="presentation" onclick={close}>
	<div
		class="w-full max-w-5xl space-y-4 rounded-lg border border-gray-700 bg-gray-900 p-4 shadow-2xl"
		role="dialog"
		aria-modal="true"
		aria-label="Publish this meeting"
		tabindex="-1"
		onclick={(e) => e.stopPropagation()}
		onkeydown={(e) => e.key === 'Escape' && close()}
	>
		<h3 class="text-lg font-semibold text-gray-100">Publish “{pub.meetingName}”</h3>

		{#if pub.loading || !sel}
			<p class="text-sm text-gray-500">{pub.loading ? 'Loading…' : ''}</p>
			{#if pub.error}<p class="text-sm text-red-400">{pub.error}</p>{/if}
		{:else}
			<div class="grid gap-6 lg:grid-cols-2">
				<!-- The choices -->
				<div class="space-y-4 text-sm">
					<PartPicker
						heading="Content"
						items={content}
						sel={sel as unknown as Record<string, boolean>}
						onchange={(key, value) => pub.set({ [key]: value })}
					/>

					<section aria-label="Quotes">
						<h4 class={groupHeading}>Quotes</h4>
						{#if sel.quotes.length}
							<ul class="space-y-2">
								{#each sel.quotes as q (quoteKey(q))}
									{@const url = clipUrl(q)}
									{@const span = joinedSpan(session, q)}
									<li class="rounded border border-gray-800 bg-gray-950/40 px-2 py-1.5">
										<div class="flex items-start gap-2">
											<span class="shrink-0 pt-0.5 font-mono text-[11px] text-gray-500">{clock(span.start)}</span>
											<span class="min-w-0 flex-1 whitespace-pre-line text-xs text-gray-300">“{quoteText(q)}”</span>
											<button
												onclick={() => pub.removeQuote(q)}
												aria-label="Remove this quote"
												class="shrink-0 px-1 text-gray-500 hover:text-gray-200">×</button
											>
										</div>
										<div class="mt-1 flex flex-wrap items-center gap-3 text-xs">
											<label class="flex items-center gap-1.5 text-gray-400">
												<input type="checkbox" class={check} checked={q.with_clip} onchange={(e) => pub.setClip(q, e.currentTarget.checked)} />
												Include audio clip
											</label>
											{#if q.with_clip && url}
												{#if listening === quoteKey(q)}
													<!-- svelte-ignore a11y_media_has_caption -->
													<audio src={url} controls autoplay preload="none" class="h-7 max-w-[16rem]"></audio>
												{:else}
													<button
														onclick={() => (listening = quoteKey(q))}
														aria-label="Listen to this clip"
														class="rounded border border-gray-700 px-1.5 py-0.5 text-gray-400 hover:border-gray-500 hover:text-gray-200"
														>▶</button
													>
												{/if}
											{/if}
										</div>
									</li>
								{/each}
							</ul>
							<p class="mt-1.5 text-xs text-amber-300/80">Listen before you publish: clips can't be redacted.</p>
						{:else}
							<p class="text-xs text-gray-500">Select lines in the transcript, then Add to publication.</p>
						{/if}
					</section>

					<section aria-label="People">
						<h4 class={groupHeading}>People</h4>
						{#if pub.people.length}
							<div class="mb-2 flex flex-wrap gap-x-4 gap-y-1">
								{#each pub.people as name (name)}
									<label class="flex items-center gap-2 text-gray-300">
										<input type="checkbox" class={check} checked={sel.attendees.includes(name)} onchange={() => pub.toggleAttendee(name)} />
										{name}
									</label>
								{/each}
							</div>
						{/if}
						<div class={segmented} role="group" aria-label="Names">
							{#each NAMES as [mode, label] (mode)}
								<button onclick={() => pub.set({ names: mode })} aria-pressed={sel.names === mode} class={segment(sel.names === mode)}>{label}</button>
							{/each}
						</div>
					</section>

					<section aria-label="Privacy">
						<h4 class={groupHeading}>Privacy</h4>
						<label class="flex items-center gap-2 text-gray-300">
							<input type="checkbox" class={check} checked={sel.hide_identifiers} onchange={(e) => pub.set({ hide_identifiers: e.currentTarget.checked })} />
							Hide emails, phones and account numbers
						</label>
					</section>

					<section aria-label="Where">
						<h4 class={groupHeading}>Where</h4>
						<div class={segmented} role="group" aria-label="Where">
							{#each targets as [t, label] (t)}
								<button onclick={() => pub.setTarget(t)} aria-pressed={pub.target === t} class={segment(pub.target === t)}>{label}</button>
							{/each}
						</div>
						{#if pub.target === 'local' && pub.defaults?.local_reach === 'this_device'}
							<p class="mt-1.5 text-xs text-amber-300/80">
								Only this computer can open this link. Turn on Share this computer with my team, publish a file, or set a publish host in
								Settings.
							</p>
						{:else if pub.target === 'local' && pub.defaults?.local_url}
							<p class="mt-1.5 text-xs text-gray-500">Opens for anyone who can reach {pub.defaults.local_url}.</p>
						{:else if pub.target === 'file'}
							<label class="mt-2 block max-w-xs">
								<span class="mb-1 block text-xs text-gray-500">Password (optional, at least 8 characters)</span>
								<input type="password" autocomplete="new-password" bind:value={pub.password} class={inputClass} />
							</label>
							{#if passwordIssue}
								<p class="mt-1 text-xs text-red-400">{passwordIssue}</p>
							{:else if !pub.password}
								<p class="mt-1 text-xs text-gray-500">Without a password, anyone with the file can read it.</p>
							{/if}
						{/if}
					</section>

					<div class="grid grid-cols-2 gap-3">
						<label>
							<span class={groupHeading + ' block'}>Expires</span>
							<select
								value={sel.expires_days == null ? 'never' : String(sel.expires_days)}
								onchange={(e) => pub.set({ expires_days: e.currentTarget.value === 'never' ? null : Number(e.currentTarget.value) })}
								class={inputClass}
							>
								{#each expiryChoices(pub.target, sel.expires_days) as d (d ?? 'never')}
									<option value={d == null ? 'never' : String(d)}>{expiryLabel(d)}</option>
								{/each}
							</select>
						</label>
						<label>
							<span class={groupHeading + ' block'}>Title</span>
							<input type="text" value={sel.title} oninput={(e) => pub.set({ title: e.currentTarget.value })} class={inputClass} />
						</label>
					</div>
				</div>

				<!-- What readers will see -->
				<div class="min-w-0">
					<p class="mb-1.5 flex items-center gap-2 text-[11px] uppercase tracking-wide text-gray-500">
						Preview
						{#if pub.previewing}<span class="normal-case tracking-normal text-gray-600">updating…</span>{/if}
					</p>
					<div class="max-h-[70vh] overflow-y-auto rounded-lg border border-gray-800 bg-gray-950 p-4">
						{#if manifest}
							<Meeting {manifest} loadClip={previewClip} preview />
						{:else if !pub.previewError}
							<p class="text-sm text-gray-500">Loading the preview…</p>
						{/if}
						{#if pub.previewError}<p class="mt-2 text-sm text-red-400">{pub.previewError}</p>{/if}
					</div>
				</div>
			</div>

			<!-- The live publication -->
			{#if current?.url && pub.target !== 'file'}
				<div class="space-y-1.5 rounded border border-emerald-900/60 bg-emerald-950/20 px-3 py-2">
					<p class="text-xs text-gray-400">
						{#if pub.justPublished}
							{expiresLine}
						{:else}
							Published {shortDate(current.created_at)} · version {current.version}{current.expires_at ? ` · expires ${shortDate(current.expires_at)}` : ''}{#if current.behind}<span class="text-amber-300">{' · behind'}</span>{/if}
						{/if}
					</p>
					<div class="flex gap-2">
						<input readonly value={current.url} aria-label="Link" onfocus={(e) => e.currentTarget.select()} class="{inputClass} font-mono text-xs" />
						<button onclick={() => copyText(current.url ?? '', 'Link copied')} class="shrink-0 rounded border border-gray-700 bg-gray-800 px-3 text-xs text-gray-200 hover:bg-gray-700">Copy</button>
						{#if pub.justPublished}
							<button onclick={() => openLink(current.url ?? '')} class="shrink-0 rounded border border-gray-700 bg-gray-800 px-3 text-xs text-gray-200 hover:bg-gray-700">Open</button>
						{/if}
					</div>
				</div>
			{/if}

			<div class="flex flex-wrap items-center gap-3 border-t border-gray-800 pt-3">
				<p class="mr-auto text-xs text-gray-500">The link carries the key: the host sees only encrypted data.</p>
				{#if pub.stage && working}<span class="text-xs text-gray-400">{pub.stage}</span>{/if}
				{#if current && pub.target !== 'file'}
					{#if confirmUnpublish}
						<span class="text-xs text-gray-300">Unpublish? The link stops working; copies already opened stay.</span>
						<button onclick={unpublish} disabled={working} class="text-sm text-red-400 hover:text-red-300 disabled:opacity-50"
							>{pub.busy === 'unpublish' ? 'Unpublishing…' : 'Yes'}</button
						>
						<button onclick={() => (confirmUnpublish = false)} disabled={working} class="text-sm text-gray-400 hover:text-gray-200">No</button>
					{:else}
						<button onclick={() => (confirmUnpublish = true)} disabled={working} class="text-sm text-red-400 hover:text-red-300 disabled:opacity-50">Unpublish</button>
					{/if}
				{/if}
				<button onclick={close} disabled={working} class="px-3 py-1.5 text-sm text-gray-400 hover:text-gray-200 disabled:opacity-50"
					>{pub.justPublished ? 'Close' : 'Cancel'}</button
				>
				<button
					onclick={primary}
					disabled={working || !!passwordIssue}
					class="rounded bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-60"
				>
					{primaryLabel}
				</button>
			</div>
			{#if pub.error}<p class="text-sm text-red-400">{pub.error}</p>{/if}
		{/if}
	</div>
</div>
