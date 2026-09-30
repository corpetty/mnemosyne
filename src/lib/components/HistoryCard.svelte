<script lang="ts">
	import { getHistory, recoverSession } from '$lib/api/backend.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { jobsState } from '$lib/stores/jobs.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { wsState } from '$lib/stores/websocket.svelte.js';
	import type { BackendEvent, HistoryEvent, HistoryFile, SessionHistory } from '$lib/types/index.js';

	// What this meeting is made of and what happened to it (backend services/history.py): its
	// parts with the gaps between them, the files behind them, audio recorded but not saved
	// into it yet, and a log of recordings started and stopped, saves, recoveries and more.

	const session = $derived(sessionState.activeSession);
	let history = $state<SessionHistory | null>(null);
	let open = $state(false);
	let recovering = $state(false);
	let now = $state(Date.now());

	let loadedFor: string | null = null;
	let seq = 0;
	async function load(id: string) {
		const mine = ++seq;
		try {
			const h = await getHistory(id);
			if (mine === seq && sessionState.activeSession?.id === id) history = h;
		} catch {
			/* a backend without it, or the meeting is gone: the card stays hidden */
		}
	}

	$effect(() => {
		const id = session?.id ?? null;
		if (id === loadedFor) return;
		loadedFor = id;
		history = null;
		open = false;
		if (id) void load(id);
	});

	// Reload when something happens to this meeting (a few events can come at once).
	let timer: ReturnType<typeof setTimeout> | undefined;
	$effect(() =>
		wsState.onMessage((raw) => {
			const msg = raw as BackendEvent;
			const id = sessionState.activeSession?.id;
			if (!id || !('session_id' in msg) || msg.session_id !== id) return;
			if (msg.type !== 'history' && msg.type !== 'session') return;
			clearTimeout(timer);
			timer = setTimeout(() => load(id), 400);
		})
	);

	// The part being recorded grows by the second.
	$effect(() => {
		if (!history?.recording_now) return;
		const t = setInterval(() => (now = Date.now()), 1000);
		return () => clearInterval(t);
	});

	const problems = $derived(
		history ? history.pending.length + history.missing.length + history.orphans.length : 0
	);
	// Something to look at: open by itself.
	$effect(() => {
		if (history && (history.pending.length || history.missing.length)) open = true;
	});

	function dur(seconds: number | null | undefined): string {
		if (seconds == null) return '?';
		const s = Math.max(0, Math.round(seconds));
		const h = Math.floor(s / 3600);
		const m = Math.floor((s % 3600) / 60);
		const ss = String(s % 60).padStart(2, '0');
		return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
	}
	const clock = (iso: string | null) =>
		iso ? new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '?';
	function size(n: number | null): string {
		if (n == null) return 'missing';
		if (n < 1024 * 1024) return `${Math.max(1, Math.round(n / 1024))} KB`;
		return `${(n / 1024 / 1024).toFixed(1)} MB`;
	}
	const partName = (n: number | null | undefined) => (n == null ? 'a part' : `part ${n + 1}`);
	const liveSeconds = (started: string | null, seconds: number | null) =>
		started ? (now - new Date(started).getTime()) / 1000 : seconds;

	const HOW: Record<string, string> = {
		recorded: 'recorded',
		recovered: 'recovered after an interruption',
		imported: 'imported file',
		combined: 'from a combined meeting',
		recording: 'recording now'
	};
	const STOP: Record<string, string> = {
		stop: 'stopped',
		capture_restart: 'stopped to restart the capture',
		app_gone: 'stopped: the app had closed',
		shutdown: 'stopped: the backend shut down',
		browser_gone: 'stopped: the browser sent no audio for 10 minutes'
	};

	const CONSENT: Record<string, string> = {
		all_parties: 'Everyone on the call was told and agreed to the recording',
		in_person: 'Everyone in the room was told about the recording',
		one_party: 'Recorded with one-party consent'
	};
	const ACCESS: Record<string, string> = { viewed: 'Opened', played: 'Listened to', exported: 'Exported' };

	function describe(e: HistoryEvent): string {
		const d = e.detail as Record<string, unknown>;
		const p = partName(e.part);
		const len = d.seconds != null ? ` (${dur(d.seconds as number)})` : '';
		switch (e.kind) {
			case 'created':
				return 'Meeting created';
			case 'recording_started': {
				const devices = (d.devices as string[] | undefined)?.join(', ');
				return `Recording ${p} started${d.restart ? ' again after a capture problem' : ''}${devices ? ` from ${devices}` : ''}`;
			}
			case 'recording_stopped':
				return `Recording ${p} ${STOP[d.reason as string] ?? 'stopped'}${len}`;
			case 'part_saved':
				return `Saved ${p}${len}`;
			case 'save_failed':
				return `Saving ${p} failed: ${d.error}`;
			case 'interrupted':
				return `Recording ${p} was interrupted (the app or backend ended while recording)`;
			case 'recovered':
				return `Recovered ${p}${len}`;
			case 'recover_empty':
				return `Nothing to recover for ${p}: its recording had no audio`;
			case 'recover_failed':
				return `Recovering failed: ${d.error}`;
			case 'capture_problem':
				return `${d.device}: ${d.message}`;
			case 'capture_ok':
				return `${d.device}: ${d.message}`;
			case 'imported':
				return `Added ${d.file} as ${p}${len}`;
			case 'combined':
				return `Combined with “${d.other_name}”`;
			case 'transcribed':
				return `Transcribed (${d.segments} lines)`;
			case 'transcribe_failed':
				return `Transcription failed: ${d.error}`;
			case 'summarized':
				return `Summarized with ${d.provider}${d.model ? `/${d.model}` : ''}`;
			case 'summarize_failed':
				return `Summary failed: ${d.error}`;
			case 'hubspot_pushed': {
				const who = (d.contacts as string[] | undefined)?.join(', ');
				return `${d.created ? 'Sent to' : 'Updated in'} HubSpot${who ? ` for ${who}` : ''}${d.auto ? ' after the summary' : ''}`;
			}
			case 'hubspot_failed':
				return `HubSpot${d.auto ? ' (after the summary)' : ''}: ${d.error}`;
			case 'audio_deleted':
				return `Audio deleted${d.reason === 'retention' ? ' by the retention rule' : ''}`;
			case 'legal_hold':
				return `Put on legal hold (${d.reason})${d.by ? ` by ${d.by}` : ''}`;
			case 'legal_hold_lifted':
				return `Legal hold lifted${d.by ? ` by ${d.by}` : ''} (was: ${d.reason})`;
			case 'consent':
				return `${CONSENT[d.consent as string] ?? 'Consent recorded'}${d.by ? ` (${d.by})` : ''}`;
			case 'viewed':
			case 'played':
			case 'exported':
				// A firm's server notes who looked at a meeting (backend services/history.py).
				return `${ACCESS[e.kind]} by ${d.by}${d.role && d.role !== 'advisor' ? ` (${d.role})` : ''}`;
			default:
				return e.kind.replaceAll('_', ' ');
		}
	}
	const bad = (kind: string) =>
		['save_failed', 'interrupted', 'recover_failed', 'capture_problem', 'transcribe_failed', 'summarize_failed', 'hubspot_failed'].includes(kind);

	const fileLine = (f: HistoryFile) =>
		[f.source, f.device_name, f.name].filter(Boolean).join(' · ');

	async function recover() {
		if (!session || recovering) return;
		recovering = true;
		try {
			jobsState.track(await recoverSession(session.id));
			toastState.info('Saving the waiting audio into the meeting');
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not recover the audio');
		} finally {
			recovering = false;
		}
	}
</script>

{#if session && history && (history.parts.length || history.pending.length || history.events.length > 1)}
	<section class="rounded-lg border {history.pending.length || history.missing.length ? 'border-amber-800/70' : 'border-gray-800'} bg-gray-900/40 p-3 space-y-3" aria-label="Parts and history">
		<button onclick={() => (open = !open)} aria-expanded={open} class="flex w-full flex-wrap items-baseline gap-x-2 gap-y-1 text-left">
			<h4 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500">Parts &amp; history</h4>
			<span class="text-xs text-gray-400">
				{history.parts.length} part{history.parts.length === 1 ? '' : 's'} · {dur(history.recorded_seconds)} recorded{#if history.gap_seconds >= 1}&nbsp;· {dur(history.gap_seconds)} not recorded between parts{/if}
			</span>
			{#if history.pending.length}
				<span class="rounded bg-amber-900/60 px-1.5 text-xs text-amber-200">{history.pending.length} recording{history.pending.length === 1 ? '' : 's'} not saved yet</span>
			{/if}
			{#if history.missing.length}
				<span class="rounded bg-red-900/60 px-1.5 text-xs text-red-200">{history.missing.length} file{history.missing.length === 1 ? '' : 's'} missing</span>
			{/if}
			{#if !problems}<span class="text-xs text-green-500/80">all audio in place</span>{/if}
			<span class="ml-auto text-xs text-gray-600">{open ? 'Hide' : 'Show'}</span>
		</button>

		{#if open}
			{#if history.pending.length}
				<div class="rounded border border-amber-800/60 bg-amber-950/30 p-2 space-y-1.5">
					{#each history.pending as p (p.recording_id ?? p.part)}
						<p class="text-sm text-amber-100">
							{dur(p.seconds)} of audio recorded as {partName(p.part)} is not in the meeting yet{p.state === 'saving' ? '; saving it now…' : '.'}
						</p>
						<ul class="text-xs text-amber-200/70">
							{#each p.files as f (f.name)}<li>{fileLine(f)} · {size(f.size)}</li>{/each}
						</ul>
					{/each}
					{#if history.pending.some((p) => p.state === 'waiting')}
						<button
							onclick={recover}
							disabled={recovering || history.recording_now}
							title={history.recording_now ? 'After this recording stops' : 'Add it to the meeting as its next part'}
							class="rounded bg-amber-700 px-2.5 py-1 text-xs font-medium text-white hover:bg-amber-600 disabled:opacity-50"
						>Save it into the meeting</button>
					{/if}
				</div>
			{/if}

			{#if history.parts.length}
				<ol class="space-y-1.5" aria-label="Parts">
					{#each history.parts as part (part.part)}
						{#if part.gap_before && part.gap_before >= 1}
							<li class="pl-3 text-xs text-gray-500 border-l-2 border-dashed border-gray-700">
								{dur(part.gap_before)} not recorded
							</li>
						{/if}
						<li class="rounded border border-gray-800 bg-gray-900/60 px-2.5 py-1.5">
							<div class="flex flex-wrap items-baseline gap-x-2 text-sm">
								<span class="font-medium text-gray-200">Part {part.part + 1}</span>
								{#if part.started_at}
									<span class="text-gray-400">
										{part.approximate ? '~' : ''}{clock(part.started_at)}–{part.how === 'recording' ? 'now' : `${part.approximate ? '~' : ''}${clock(part.ended_at)}`}
									</span>
								{/if}
								<span class="text-gray-400">{dur(part.how === 'recording' ? liveSeconds(part.started_at, part.seconds) : part.seconds)}</span>
								<span class="text-xs {part.how === 'recording' ? 'text-red-400' : part.how === 'recovered' ? 'text-amber-300' : 'text-gray-500'}">{HOW[part.how]}</span>
								{#if part.how !== 'recording'}<span class="ml-auto text-xs text-gray-600">at {dur(part.offset)} in the meeting</span>{/if}
							</div>
							<ul class="mt-0.5 text-xs text-gray-500">
								{#each part.files as f (f.name)}
									<li class={f.size == null ? 'text-red-400' : ''}>{fileLine(f)} · {size(f.size)}</li>
								{/each}
							</ul>
						</li>
					{/each}
				</ol>
			{/if}

			{#if history.missing.length}
				<div class="text-xs text-red-300">
					<p class="font-medium">Missing from the meeting's folder:</p>
					<ul>{#each history.missing as name (name)}<li>{name}</li>{/each}</ul>
				</div>
			{/if}
			{#if history.orphans.length}
				<div class="text-xs text-gray-400">
					<p class="font-medium">Audio in the meeting's folder that it does not use:</p>
					<ul>{#each history.orphans as f (f.name)}<li>{f.name} · {size(f.size)}</li>{/each}</ul>
				</div>
			{/if}

			<div>
				<h5 class="mb-1 text-[11px] font-semibold uppercase tracking-wide text-gray-500">What happened</h5>
				<ol class="space-y-0.5 text-xs" aria-label="What happened">
					{#each history.events as e, i (i)}
						<li class="flex gap-2">
							<span class="w-24 shrink-0 whitespace-nowrap font-mono text-gray-600" title={new Date(e.at).toLocaleString()}>{clock(e.at)}</span>
							<span class={bad(e.kind) ? 'text-amber-300' : 'text-gray-300'}>{describe(e)}</span>
						</li>
					{/each}
				</ol>
				{#if history.events.length <= 1 && history.parts.length}
					<p class="mt-1 text-xs text-gray-600">This meeting was recorded before the log began; its parts are worked out from its recordings (~ marks estimated times).</p>
				{/if}
			</div>
		{/if}
	</section>
{/if}
