<script module lang="ts">
	import type { ProviderModels as PM, SummaryStyle as SS } from '$lib/types/index.js';
	// Survives tab switches so the pickers do not flash empty while models are re-listed.
	let cachedProviders: PM[] | null = null;
	let cachedStyles: SS[] | null = null;
</script>

<script lang="ts">
	import { copyText } from '$lib/app/clipboard.js';
	import { listModels, listSummaryStyles, summarizeSession, getSettings, getSystemInfo } from '$lib/api/backend.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import { jobsState } from '$lib/stores/jobs.svelte.js';
	import { uiState } from '$lib/stores/ui.svelte.js';
	import { playerState } from '$lib/stores/player.svelte.js';
	import Markdown from './Markdown.svelte';
	import ExternalNotesCard from './ExternalNotesCard.svelte';
	import SummaryEditor from './SummaryEditor.svelte';
	import SummaryVersions from './SummaryVersions.svelte';
	import HubSpotCard from './HubSpotCard.svelte';
	import CommentThread from './CommentThread.svelte';
	import CommentComposer from './CommentComposer.svelte';
	import SummaryComments from './SummaryComments.svelte';
	import { commentsState } from '$lib/stores/comments.svelte.js';
	import { tick } from 'svelte';
	import { wsState } from '$lib/stores/websocket.svelte.js';
	import {
		getSessionLinks,
		linkDecisions,
		listDecisions,
		unlinkDecisionMention,
		createIssues,
		draftFollowup,
		getIntegrations,
		saveFollowup,
		sendFollowup,
		setActionItemDone,
		type DestinationName,
		type TrackerName
	} from '$lib/api/backend.js';

	let selected = $state<Set<number>>(new Set());
	let creating = $state(false);
	let editing = $state(false);
	let showVersions = $state(false);
	let lastSession: string | null = null;
	$effect(() => {
		const id = sessionState.activeSession?.id ?? null;
		if (id !== lastSession) {
			lastSession = id;
			selected = new Set();
			editing = false;
			showVersions = false;
			commentPanel = null;
			bodyDraft = null;
			selectionAnchor = null;
		}
	});

	// Comments on decisions, action items and questions: one inline panel open at a time.
	type BlockKind = 'decision' | 'item' | 'question';
	let commentPanel = $state<string | null>(null);
	function toggleComments(id: string) {
		commentPanel = commentPanel === id ? null : id;
	}
	function panelKey(e: KeyboardEvent) {
		// The composer's Esc cancels (and closes); an edit's Esc only ends the edit.
		if (e.key === 'Escape' && !e.defaultPrevented) commentPanel = null;
	}
	let summaryRoot = $state<HTMLElement | null>(null);
	/** "Go to" from the Comments card: open the block's panel and bring it into view. */
	async function goToBlock(id: string) {
		commentPanel = id;
		await tick();
		summaryRoot
			?.querySelector<HTMLElement>(`[data-comment-block="${CSS.escape(id)}"]`)
			?.scrollIntoView({ block: 'center', behavior: 'smooth' });
	}
	let commentsFilter = $state<'open' | 'resolved' | 'all'>('open');
	// Search hand-off: a comment on the summary opens its block's panel, or shows its thread
	// in the Comments card. Roots on transcript lines are the Transcript view's.
	$effect(() => {
		const id = uiState.focusComment;
		if (!id || !summaryRoot || !sessionState.activeSession?.summary_data) return;
		if (commentsState.sessionId !== sessionState.activeSession.id) return;
		if (commentsState.threads.length === 0) return;
		const t = commentsState.threads.find((x) => x.root.id === id || x.replies.some((r) => r.id === id));
		if (!t || t.root.segment_idx != null || t.root.target === 'segment') return;
		uiState.focusComment = null;
		const r = t.root;
		if (!r.detached && r.target_id && commentsState.byBlock.has(r.target_id)) {
			void goToBlock(r.target_id);
			return;
		}
		if (r.resolved_at && commentsFilter === 'open') commentsFilter = 'resolved';
		void tick().then(() =>
			summaryRoot
				?.querySelector<HTMLElement>(`[data-comment-thread="${CSS.escape(r.id)}"]`)
				?.scrollIntoView({ block: 'center', behavior: 'smooth' })
		);
	});
	function blockCounts(id: string): { total: number; open: number } {
		const ts = commentsState.byBlock.get(id) ?? [];
		return { total: ts.length, open: ts.filter((t) => !t.root.resolved_at && !t.root.deleted).length };
	}

	// Comments on a selection of the rendered summary text.
	type Quote = { exact: string; prefix: string; suffix: string };
	const CONTEXT = 32; // characters of rendered text kept before and after a selection
	const collapse = (t: string) => t.replace(/\s+/g, ' ').trim();
	let summaryCard = $state<HTMLElement | null>(null);
	let summaryText = $state<HTMLElement | null>(null);
	let selectionAnchor = $state<(Quote & { top: number; left: number }) | null>(null);
	let bodyDraft = $state<Quote | null>(null);

	function onSelectionChange() {
		const sel = document.getSelection();
		if (!commentsState.canComment || !summaryCard || !summaryText || !sel || sel.isCollapsed || !sel.rangeCount) {
			selectionAnchor = null;
			return;
		}
		const range = sel.getRangeAt(0);
		if (!summaryText.contains(range.startContainer) || !summaryText.contains(range.endContainer)) {
			selectionAnchor = null;
			return;
		}
		const exact = collapse(sel.toString());
		if (!exact) {
			selectionAnchor = null;
			return;
		}
		// The rendered text before and after the selection (what Range.toString / textContent see).
		const before = document.createRange();
		before.selectNodeContents(summaryText);
		before.setEnd(range.startContainer, range.startOffset);
		const after = document.createRange();
		after.selectNodeContents(summaryText);
		after.setStart(range.endContainer, range.endOffset);
		const prefix = collapse(before.toString()).slice(-CONTEXT).trimStart();
		const suffix = collapse(after.toString()).slice(0, CONTEXT).trimEnd();
		// Just above where the selection ends.
		const rects = range.getClientRects();
		const end = rects.length ? rects[rects.length - 1] : range.getBoundingClientRect();
		const card = summaryCard.getBoundingClientRect();
		selectionAnchor = {
			exact,
			prefix,
			suffix,
			top: Math.max(0, end.top - card.top - 28),
			left: Math.min(Math.max(0, end.right - card.left - 36), Math.max(0, card.width - 80))
		};
	}

	function commentOnSelection() {
		if (!selectionAnchor) return;
		const { exact, prefix, suffix } = selectionAnchor;
		bodyDraft = { exact, prefix, suffix };
		selectionAnchor = null;
		document.getSelection()?.removeAllRanges();
	}

	let followupStyle = $state<'email' | 'chat'>('email');
	let followupText = $state('');
	let followupFor = '';
	// Show the saved draft for this session (and pick up a new one when it lands).
	$effect(() => {
		const s = sessionState.activeSession;
		const saved = s?.summary_data?.followup ?? '';
		const key = `${s?.id}:${saved}`;
		if (key !== followupFor) {
			followupFor = key;
			followupText = saved;
		}
	});
	$effect(() =>
		jobsState.onComplete((job) => {
			if (job.kind === 'followup' && job.session_id === sessionState.activeSession?.id) sessionState.refreshActive();
		})
	);
	const followupError = $derived.by(() => {
		const last = sessionState.activeSession ? jobsState.last(sessionState.activeSession.id, 'followup') : null;
		return last?.status === 'failed' ? last.error : null;
	});
	const followupJob = $derived(
		sessionState.activeSession ? jobsState.active(sessionState.activeSession.id, 'followup') : null
	);

	async function handleFollowup() {
		const session = sessionState.activeSession;
		if (!session) return;
		try {
			jobsState.track(await draftFollowup(session.id, followupStyle));
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not draft a follow-up');
		}
	}

	let savingFollowup = $state(false);
	async function keepFollowup() {
		const session = sessionState.activeSession;
		if (!session) return;
		savingFollowup = true;
		try {
			await saveFollowup(session.id, followupText);
			await sessionState.refreshActive();
			toastState.success('Follow-up draft saved');
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not save the draft');
		} finally {
			savingFollowup = false;
		}
	}

	async function copyFollowup() {
		await copyText(followupText, 'Follow-up copied');
	}

	async function toggleDone(i: number, done: boolean) {
		const session = sessionState.activeSession;
		if (!session) return;
		try {
			await setActionItemDone(session.id, i, done);
			await sessionState.refreshActive();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not update the item');
		}
	}

	function toggle(i: number) {
		const next = new Set(selected);
		if (next.has(i)) next.delete(i);
		else next.add(i);
		selected = next;
	}

	function fmtTime(sec: number): string {
		const m = Math.floor(sec / 60);
		const s = Math.floor(sec % 60);
		return m >= 60
			? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}:${String(s).padStart(2, '0')}`
			: `${m}:${String(s).padStart(2, '0')}`;
	}

	/** Jump to the transcript line at `start` (a chapter, or where a summary item came up). */
	function openChapter(start: number) {
		const session = sessionState.activeSession;
		if (!session) return;
		const idx = session.transcript.findIndex((seg) => seg.start >= start - 0.01);
		sessionState.pendingOpen = { sessionId: session.id, idx: idx < 0 ? null : idx };
		if (playerState.available) playerState.seek(start, false);
	}

	async function openLink(href: string) {
		try {
			const { invoke } = await import('@tauri-apps/api/core');
			await invoke('plugin:shell|open', { path: href });
		} catch {
			window.open(href, '_blank', 'noopener,noreferrer');
		}
	}

	const TRACKER_LABEL: Record<TrackerName, string> = { github: 'GitHub', linear: 'Linear', jira: 'Jira' };
	let trackers = $state<TrackerName[]>([]);
	let destinations = $state<DestinationName[]>([]);
	let hubspot = $state(false);
	let tracker = $state<TrackerName>('github');
	$effect(() => {
		getIntegrations()
			.then((r) => {
				trackers = r.trackers;
				destinations = r.destinations;
				hubspot = r.crm.includes('hubspot');
				if (r.trackers.length && !r.trackers.includes(tracker)) tracker = r.trackers[0];
			})
			.catch(() => {});
	});

	async function handleCreateIssues() {
		const session = sessionState.activeSession;
		if (!session || selected.size === 0) return;
		if (!trackers.includes(tracker)) {
			toastState.error('Set up GitHub, Linear or Jira in Settings first');
			return;
		}
		const n = selected.size;
		const where = TRACKER_LABEL[tracker];
		if (!confirm(`Create ${n} issue${n > 1 ? 's' : ''} in ${where}?`)) return;
		creating = true;
		try {
			const r = await createIssues(session.id, tracker, [...selected].sort((a, b) => a - b));
			selected = new Set();
			await sessionState.refreshActive();
			if (r.created.length) toastState.success(`Created ${r.created.length} issue${r.created.length > 1 ? 's' : ''} in ${where}`);
			for (const err of r.errors) toastState.error(err);
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not create issues');
		} finally {
			creating = false;
		}
	}

	async function postFollowup(destination: DestinationName) {
		const session = sessionState.activeSession;
		if (!session || !followupText.trim()) return;
		const where = destination === 'slack' ? 'Slack' : 'the Matrix room';
		if (!confirm(`Post this follow-up to ${where}?`)) return;
		try {
			const r = await sendFollowup(session.id, destination, followupText);
			toastState.success(r.message);
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not post');
		}
	}
	import type { DecisionMention, DecisionObject, ProviderModels, SessionLinks, SummaryStyle } from '$lib/types/index.js';
	import { groupFacts } from '$lib/app/facts.js';
	import { canChange } from '$lib/app/access.js';

	let providers = $state<ProviderModels[]>(cachedProviders ?? []);
	let styles = $state<SummaryStyle[]>(cachedStyles ?? []);
	let optionsLoading = $state(cachedProviders === null);
	let selectedProvider = $state('');
	let selectedModel = $state('');
	let selectedStyle = $state('');
	let error = $state('');
	let loaded = $state(false);
	let loadFailed = $state(false);
	let submitting = $state(false);
	// A sidecar that merges hand edits is installed: summarizing again keeps them, else it replaces them.
	let summaryMerge = $state(false);

	async function load() {
		loadFailed = false;
		getSystemInfo()
			.then((s) => (summaryMerge = s.summary_merge))
			.catch(() => {});
		optionsLoading = cachedProviders === null;
		try {
			const [p, s, settings] = await Promise.all([listModels(), listSummaryStyles(), getSettings()]);
			providers = cachedProviders = p;
			styles = cachedStyles = s;
			optionsLoading = false;
			selectedProvider = settings.values.default_provider || p[0]?.provider || '';
			selectedModel = settings.values.default_model || '';
			selectedStyle = settings.values.summary_style || 'meeting';
			if (!selectedModel) {
				const prov = providers.find((x) => x.provider === selectedProvider);
				if (prov?.models.length) selectedModel = prov.models[0];
			}
			loaded = true;
		} catch (e) {
			console.error('Failed to load summary options:', e);
			optionsLoading = false; // not "Loading models…" forever
			loadFailed = true;
		}
	}

	function availableModels(): string[] {
		return providers.find((p) => p.provider === selectedProvider)?.models ?? [];
	}

	function handleProviderChange() {
		const models = availableModels();
		selectedModel = models.length > 0 ? models[0] : '';
	}

	async function handleSummarize() {
		const session = sessionState.activeSession;
		if (!session || submitting) return;
		if (
			!summaryMerge &&
			session.summary_data?.edited_at &&
			!confirm(
				'This summary was edited by hand. Summarizing again replaces your edits (done ticks, issue links and due dates carry over). Continue?'
			)
		)
			return;
		error = '';
		submitting = true; // until the job is tracked: a double click queues one summary
		try {
			const job = await summarizeSession(session.id, selectedProvider, selectedModel, selectedStyle);
			jobsState.track(job);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Summarization failed';
		} finally {
			submitting = false;
		}
	}

	// Revise the current summary (hand edits included) as asked, with the chosen model.
	let reviseText = $state('');
	async function handleRevise() {
		const session = sessionState.activeSession;
		const ask = reviseText.trim();
		if (!session || !ask || submitting) return;
		error = '';
		submitting = true;
		try {
			jobsState.track(await summarizeSession(session.id, selectedProvider, selectedModel, selectedStyle, ask));
			reviseText = '';
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not revise the summary';
		} finally {
			submitting = false;
		}
	}

	async function copyToClipboard() {
		const s = sessionState.activeSession;
		if (!s?.summary) return;
		const d = s.summary_data;
		let text = s.summary;
		if (d) {
			if (d.decisions.length) text += '\n\n## Decisions\n' + d.decisions.map((x) => `- ${x}`).join('\n');
			if (d.action_items.length)
				text += '\n\n## Action Items\n' + d.action_items.map((a) => `- [ ] ${a.text}${a.owner ? ` (${a.owner})` : ''}`).join('\n');
			if (d.open_questions.length) text += '\n\n## Open Questions\n' + d.open_questions.map((x) => `- ${x}`).join('\n');
			if (factGroups.length)
				text +=
					'\n\n## Client Facts' +
					factGroups.map((g) => `\n\n### ${g.heading}\n` + g.facts.map((f) => `- ${f.text}`).join('\n')).join('');
		}
		await copyText(text, 'Summary copied');
	}

	$effect(() => {
		if (!loaded) load();
	});

	const data = $derived(sessionState.activeSession?.summary_data ?? null);

	// The same tasks and decisions in other meetings.
	let links = $state<SessionLinks | null>(null);
	async function loadLinks() {
		const id = sessionState.activeSession?.id;
		if (!id) {
			links = null;
			return;
		}
		try {
			const r = await getSessionLinks(id);
			if (sessionState.activeSession?.id === id) links = r;
		} catch {
			if (sessionState.activeSession?.id !== id) links = null;
		}
	}
	$effect(() => {
		void sessionState.activeSession?.id;
		void sessionState.activeSession?.summary_data; // reload after the summary changes
		loadLinks();
	});
	$effect(() =>
		wsState.onMessage((raw) => {
			const msg = raw as { type?: string; session_id?: string };
			if (msg.type === 'session') loadLinks();
		})
	);

	const linkDate = (m: { created_at: string }) =>
		new Date(m.created_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
	// The same decision taken again reads as such, not as superseding itself (exact links).
	const words = (t: string) => t.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
	const sameWords = (a: string, b: string) => words(a) === words(b);
	function openMeeting(sessionId: string) {
		sessionState.pendingOpen = null;
		sessionState.selectSession(sessionId);
	}
	const decisionId = (i: number) => data?.decision_ids?.[i] ?? '';

	async function unlinkDecision(key: string, did: string) {
		const session = sessionState.activeSession;
		if (!session) return;
		try {
			await unlinkDecisionMention(key, session.id, did);
			await loadLinks();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not unlink the decision');
		}
	}

	// "Supersedes…": pick an earlier decision from another meeting.
	let pickFor = $state<string | null>(null);
	let pickQuery = $state('');
	let pickAll = $state<DecisionObject[]>([]);
	async function startPick(did: string) {
		pickFor = did;
		pickQuery = '';
		try {
			pickAll = await listDecisions();
		} catch (e) {
			pickAll = [];
			toastState.error(e instanceof Error ? e.message : 'Could not list decisions');
		}
	}
	const pickResults = $derived.by(() => {
		const sid = sessionState.activeSession?.id;
		const q = pickQuery.trim().toLowerCase();
		return pickAll
			.filter((o) => o.mentions.length && o.mentions.every((m) => m.session_id !== sid))
			.filter((o) => !q || o.text.toLowerCase().includes(q) || o.mentions[0].session_name.toLowerCase().includes(q))
			.slice(0, 8);
	});
	async function pick(did: string, o: DecisionObject) {
		const session = sessionState.activeSession;
		if (!session) return;
		const head = o.mentions[0];
		try {
			await linkDecisions(
				{ session_id: session.id, decision_id: did },
				{ session_id: head.session_id, decision_id: head.decision_id }
			);
			pickFor = null;
			await loadLinks();
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not link the decision');
		}
	}
	const factGroups = $derived(groupFacts(data?.client_facts ?? []));
	// Someone else's meeting (a reviewer reading a member's): no controls the server refuses.
	const mine = $derived(canChange(sessionState.activeSession));
	const liveNotes = $derived(sessionState.activeSession?.copilot_notes ?? null);
	const activeJob = $derived(
		sessionState.activeSession ? jobsState.active(sessionState.activeSession.id, 'summarize') : null
	);
	const lastJob = $derived(
		sessionState.activeSession ? jobsState.last(sessionState.activeSession.id, 'summarize') : null
	);
	const jobError = $derived(lastJob?.status === 'failed' ? lastJob.error : null);
</script>

{#snippet at(seconds: number | null | undefined)}
	{#if seconds != null}
		<button
			onclick={() => openChapter(seconds)}
			class="ml-1.5 font-mono text-[11px] text-blue-400 hover:text-blue-300"
			title="Show where this came up in the transcript"
		>{fmtTime(seconds)}</button>
	{/if}
{/snippet}

{#snippet decisionLine(kind: 'supersedes' | 'superseded by', m: DecisionMention, key: string, did: string, text: string)}
	{@const same = sameWords(m.text, text)}
	<div class="text-[11px] text-gray-500 list-none">
		{same ? (kind === 'supersedes' ? 'also decided in' : 'decided again in') : kind}
		<button onclick={() => openMeeting(m.session_id)} class="text-left hover:text-gray-300" title="Open that meeting">{#if same}{m.session_name}, {linkDate(m)}{:else}“{m.text}” ({m.session_name}, {linkDate(m)}){/if}</button>
		{#if mine}<button onclick={() => unlinkDecision(key, did)} class="ml-1 text-gray-600 hover:text-gray-300 underline">Unlink</button>{/if}
	</div>
{/snippet}

{#snippet commentControls(id: string, what: string)}
	{#if id}
		{@const c = blockCounts(id)}
		{#if c.total}
			<button
				onclick={() => toggleComments(id)}
				title="{c.total} comment{c.total === 1 ? '' : 's'}{c.open && c.open < c.total ? `, ${c.open} open` : ''}"
				aria-label="{c.total} comment{c.total === 1 ? '' : 's'} on {what}"
				aria-expanded={commentPanel === id}
				class="ml-1.5 text-[11px] rounded-full bg-gray-800 px-1.5 {c.open ? 'text-sky-300' : 'text-gray-500'}"
			>{c.open || c.total}</button>
		{/if}
		{#if commentsState.canComment}
			<button
				onclick={() => toggleComments(id)}
				aria-label="Comment on {what}"
				class="ml-2 text-[11px] text-gray-600 hover:text-gray-300 opacity-0 group-hover:opacity-100 focus:opacity-100">Comment</button>
		{/if}
	{/if}
{/snippet}

{#snippet commentPanelFor(kind: BlockKind, id: string)}
	{#if id && commentPanel === id && sessionState.activeSession}
		{@const sid = sessionState.activeSession.id}
		<div class="mt-1.5 mb-1 space-y-1.5 list-none text-left" role="presentation" onkeydown={panelKey}>
			{#each commentsState.byBlock.get(id) ?? [] as t (t.root.id)}
				<CommentThread thread={t} sessionId={sid} compact />
			{/each}
			{#if commentsState.canComment}
				<CommentComposer
					sessionId={sid}
					target={{ target: kind, target_id: id, exact: '', prefix: '', suffix: '', parent_id: '' }}
					autofocus
					oncancel={() => (commentPanel = null)}
				/>
			{/if}
		</div>
	{/if}
{/snippet}

<svelte:document onselectionchange={onSelectionChange} />

<div class="space-y-3">
	{#if editing && sessionState.activeSession}
		<SummaryEditor session={sessionState.activeSession} ondone={() => (editing = false)} />
	{:else if sessionState.activeSession?.summary}
		<div bind:this={summaryRoot} class="relative space-y-3">
			{#if data && data.topics.length}
				<div class="flex flex-wrap gap-1">
					{#each data.topics as t}
						<span class="text-[11px] px-2 py-0.5 rounded-full bg-gray-800 text-gray-400 border border-gray-700">{t}</span>
					{/each}
				</div>
			{/if}
			{#if showVersions}
				<SummaryVersions sessionId={sessionState.activeSession.id} canRestore={mine} onclose={() => (showVersions = false)} />
			{/if}
			<div bind:this={summaryCard} class="relative bg-gray-900 border border-gray-700 rounded-lg p-4">
				<div bind:this={summaryText}><Markdown text={sessionState.activeSession.summary} /></div>
				{#if selectionAnchor}
					<button
						onmousedown={(e) => e.preventDefault()}
						onclick={commentOnSelection}
						style="top: {selectionAnchor.top}px; left: {selectionAnchor.left}px"
						class="absolute z-10 px-2 py-0.5 text-[11px] rounded bg-gray-800 border border-gray-600 text-gray-200 hover:bg-gray-700 shadow"
					>Comment</button>
				{/if}
			</div>
			{#if bodyDraft && commentsState.canComment}
				<div class="bg-gray-900 border border-gray-700 rounded-lg p-3 space-y-1.5">
					<p class="text-xs text-gray-400 italic border-l-2 border-gray-600 pl-2">“{bodyDraft.exact.length > 240 ? `${bodyDraft.exact.slice(0, 240)}…` : bodyDraft.exact}”</p>
					<CommentComposer
						sessionId={sessionState.activeSession.id}
						target={{ target: 'body', target_id: '', parent_id: '', ...bodyDraft }}
						placeholder="Comment on this"
						autofocus
						onposted={() => (bodyDraft = null)}
						oncancel={() => (bodyDraft = null)}
					/>
				</div>
			{/if}
			{#if data}
				<div class="grid gap-3 md:grid-cols-2">
					{#if data.decisions.length}
						<section class="bg-gray-900 border border-gray-700 rounded-lg p-3">
							<h4 class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-2">Decisions</h4>
							<ul class="space-y-1 text-sm text-gray-200 list-disc list-inside">
								{#each data.decisions as d, i}
										{@const did = decisionId(i)}
										{@const dl = did ? links?.decisions[did] : undefined}
										<li class="group" data-comment-block={did || undefined}>{d}{@render at(data.decision_at[i])}{#if mine && did}<button onclick={() => (pickFor === did ? (pickFor = null) : startPick(did))} class="ml-2 text-[11px] text-gray-600 hover:text-gray-300 opacity-0 group-hover:opacity-100 focus:opacity-100">Supersedes…</button>{/if}{@render commentControls(did, 'this decision')}
											{#if dl?.earlier.length}{@render decisionLine('supersedes', dl.earlier[0], dl.key, did, d)}{/if}
											{#if dl?.later.length}{@render decisionLine('superseded by', dl.later[0], dl.key, did, d)}{/if}
											{#if pickFor === did}
												<div class="mt-1 space-y-1 list-none" role="presentation" onkeydown={(e) => { if (e.key === 'Escape') pickFor = null; }}>
													<input bind:value={pickQuery} placeholder="Find an earlier decision…" aria-label="Find an earlier decision" class="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200 placeholder-gray-500" />
													{#each pickResults as o (o.key)}
														<button onclick={() => pick(did, o)} class="block w-full text-left text-xs text-gray-300 hover:bg-gray-800 rounded px-1.5 py-0.5">“{o.text}” · {o.mentions[0].session_name} · {linkDate(o.mentions[0])}</button>
													{:else}
														<p class="text-[11px] text-gray-600">No earlier decisions found.</p>
													{/each}
													<button onclick={() => (pickFor = null)} class="text-[11px] text-gray-500 hover:text-gray-300">Cancel</button>
												</div>
											{/if}
											{@render commentPanelFor('decision', did)}
										</li>
									{/each}
							</ul>
						</section>
					{/if}
					{#if data.action_items.length}
						<section class="bg-gray-900 border border-gray-700 rounded-lg p-3">
							<h4 class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-2">Action items</h4>
							<ul class="space-y-1 text-sm text-gray-200">
								{#each data.action_items as a, i}
									<li class="group" data-comment-block={a.id || undefined}>
									<div class="flex gap-2 items-start">
										{#if a.issue_url}
											<a href={a.issue_url} target="_blank" rel="noopener noreferrer" onclick={(e) => { e.preventDefault(); openLink(a.issue_url!); }} class="text-green-500 hover:text-green-300 text-xs mt-0.5" title="Open the GitHub issue">✓ issue</a>
										{:else if mine}
											<input type="checkbox" checked={selected.has(i)} onchange={() => toggle(i)} class="mt-1 rounded border-gray-600 bg-gray-800" title="Select to create a GitHub issue" />
										{/if}
										<button
											onclick={() => toggleDone(i, !a.done)}
											title={a.done ? 'Done · click to reopen' : 'Mark done'}
											aria-label={a.done ? `Reopen: ${a.text}` : `Mark done: ${a.text}`}
											class="text-xs mt-0.5 {a.done ? 'text-emerald-400' : 'text-gray-600 hover:text-gray-300'}"
										>{a.done ? '✓' : '○'}</button>
										<span class={a.done ? 'line-through text-gray-500' : ''}>{a.text}{#if a.owner}<span class="text-gray-500"> · {a.owner}</span>{/if}{#if a.due}<span class="text-amber-300/80 text-xs"> · due {new Date(`${a.due}T12:00:00`).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}</span>{/if}{#if a.live}<span class="ml-1.5 text-[10px] px-1 rounded bg-purple-900/50 text-purple-300" title="Heard by the live copilot during the meeting; the summary did not list it">live</span>{/if}{@render at(a.at)}{#if a.id && links?.items[a.id]}{@const il = links.items[a.id]}{#if il.earlier.length}<button onclick={() => openMeeting(il.earlier[0].session_id)} class="ml-1.5 text-[11px] text-gray-500 hover:text-gray-300" title={il.earlier.length > 1 ? `Earlier in ${il.earlier.length} meetings` : 'Open that meeting'}>↩ continues from {il.earlier[0].session_name}, {linkDate(il.earlier[0])}</button>{/if}{#if il.later.length}<button onclick={() => openMeeting(il.later[0].session_id)} class="ml-1.5 text-[11px] text-gray-500 hover:text-gray-300" title={il.later.length > 1 ? `Continued in ${il.later.length} meetings` : 'Open that meeting'}>→ continued in {il.later[0].session_name}, {linkDate(il.later[0])}</button>{/if}{/if}{@render commentControls(a.id ?? '', 'this action item')}</span>
									</div>
									{@render commentPanelFor('item', a.id ?? '')}
									</li>
								{/each}
							</ul>
							{#if selected.size > 0}
								<div class="mt-2 flex items-center gap-2">
									{#if trackers.length > 1}
										<select bind:value={tracker} aria-label="Issue tracker" class="bg-gray-800 border border-gray-700 rounded px-1.5 py-1 text-xs text-gray-200">
											{#each trackers as t}<option value={t}>{TRACKER_LABEL[t]}</option>{/each}
										</select>
									{/if}
									<button onclick={handleCreateIssues} disabled={creating} class="px-3 py-1 text-xs rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200 disabled:opacity-50">
										{creating ? 'Creating…' : `Create ${selected.size} issue${selected.size > 1 ? 's' : ''} in ${TRACKER_LABEL[tracker]}`}
									</button>
								</div>
							{/if}
						</section>
					{/if}
					{#if data.chapters.length}
						<section class="bg-gray-900 border border-gray-700 rounded-lg p-3 md:col-span-2">
							<h4 class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-2">Chapters</h4>
							<ol class="space-y-0.5 text-sm">
								{#each data.chapters as c (c.start)}
									<li>
										<button onclick={() => openChapter(c.start)} class="flex gap-3 text-left text-gray-200 hover:text-white">
											<span class="font-mono text-xs text-blue-400 pt-0.5 w-12 text-right">{fmtTime(c.start)}</span>
											<span>{c.title}</span>
										</button>
									</li>
								{/each}
							</ol>
						</section>
					{/if}
					{#if data.open_questions.length}
						<section class="bg-gray-900 border border-gray-700 rounded-lg p-3 md:col-span-2">
							<h4 class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-2">Open questions</h4>
							<ul class="space-y-1 text-sm text-gray-200 list-disc list-inside">
								{#each data.open_questions as q, i}
									{@const qid = data.question_ids?.[i] ?? ''}
									<li class="group" data-comment-block={qid || undefined}>{q}{@render at(data.question_at[i])}{@render commentControls(qid, 'this question')}{@render commentPanelFor('question', qid)}</li>
								{/each}
							</ul>
						</section>
					{/if}
					{#if factGroups.length}
						<section class="bg-gray-900 border border-gray-700 rounded-lg p-3 md:col-span-2" aria-label="Client facts">
							<h4 class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-2">Client facts</h4>
							<div class="grid gap-3 sm:grid-cols-2">
								{#each factGroups as g (g.kind)}
									<div>
										<h5 class="text-[11px] text-gray-500 mb-1">{g.heading}</h5>
										<ul class="space-y-1 text-sm text-gray-200 list-disc list-inside">
											{#each g.facts as f}<li>{f.text}{@render at(f.at)}</li>{/each}
										</ul>
									</div>
								{/each}
							</div>
						</section>
					{/if}
				</div>
			{/if}
			<SummaryComments ongoto={goToBlock} bind:filter={commentsFilter} />
			{#if data}
				<p class="text-[11px] text-gray-600">
					{data.style} summary{#if data.provider} · {data.provider}/{data.model}{/if}{#if data.edited_at} · edited {new Date(data.edited_at).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}{#if data.edited_by} by {data.edited_by}{/if}{/if}
				</p>
				{#if hubspot && mine}<HubSpotCard />{/if}
				{#if mine}
				<section class="bg-gray-900 border border-gray-700 rounded-lg p-3 space-y-2">
					<div class="flex flex-wrap items-center gap-2">
						<h4 class="text-xs font-semibold text-gray-400 uppercase tracking-wide mr-auto">Follow-up</h4>
						<select bind:value={followupStyle} class="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200" aria-label="Follow-up style">
							<option value="email">Email</option>
							<option value="chat">Chat message</option>
						</select>
						<button
							onclick={handleFollowup}
							disabled={!!followupJob}
							class="px-3 py-1 text-xs rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200 disabled:opacity-50"
						>
							{followupJob ? 'Drafting…' : data.followup ? 'Redraft' : 'Draft follow-up'}
						</button>
						{#if followupText}
							<button onclick={copyFollowup} class="px-3 py-1 text-xs rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200">Copy</button>
							{#each destinations as d}
								<button onclick={() => postFollowup(d)} class="px-3 py-1 text-xs rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200">
									Post to {d === 'slack' ? 'Slack' : 'Matrix'}
								</button>
							{/each}
						{/if}
					</div>
					{#if followupError}<p class="text-xs text-red-400">{followupError}</p>{/if}
					{#if followupText}
						{#if followupText.trim() !== data.followup}
							<button onclick={keepFollowup} disabled={savingFollowup} class="self-start px-3 py-1 text-xs rounded bg-blue-600 hover:bg-blue-700 text-white disabled:opacity-50">
								{savingFollowup ? 'Saving…' : 'Save draft'}
							</button>
						{/if}
						<textarea
							bind:value={followupText}
							rows={Math.min(16, Math.max(4, followupText.split('\n').length + 1))}
							aria-label="Follow-up draft"
							class="w-full bg-gray-950 border border-gray-800 rounded px-2 py-1.5 text-sm text-gray-200 font-sans resize-y"
						></textarea>
						<p class="text-[11px] text-gray-600">Edit it here and save it; drafting again replaces the saved draft.</p>
					{:else if !followupJob}
						<p class="text-[11px] text-gray-600">An email or chat message recapping decisions and next steps, ready to paste.</p>
					{/if}
				</section>
				{/if}
			{/if}
			<div class="absolute top-0 right-0 flex gap-1">
				<button
					onclick={() => (showVersions = !showVersions)}
					class="text-gray-500 hover:text-gray-300 text-xs px-2 py-1 rounded bg-gray-800 border border-gray-700 transition-colors"
					title="Earlier summaries, and putting one back"
				>
					Versions
				</button>
				{#if mine}
					<button
						onclick={() => (editing = true)}
						class="text-gray-500 hover:text-gray-300 text-xs px-2 py-1 rounded bg-gray-800 border border-gray-700 transition-colors"
						title="Edit the summary, topics, decisions, action items, chapters and questions"
					>
						Edit
					</button>
				{/if}
				<button
					onclick={copyToClipboard}
					class="text-gray-500 hover:text-gray-300 text-xs px-2 py-1 rounded bg-gray-800 border border-gray-700 transition-colors"
					title="Copy summary as markdown"
				>
					Copy
				</button>
			</div>
		</div>
	{:else if mine && sessionState.activeSession}
		<button onclick={() => (editing = true)} class="text-xs text-blue-400 hover:text-blue-300">Write a summary yourself</button>
	{/if}

	{#if mine && !editing}
	<div class="flex flex-wrap items-center gap-3">
		<select bind:value={selectedStyle} class="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200" title="Summary style">
			{#each styles as s}
				<option value={s.id}>{s.id}</option>
			{/each}
		</select>
		<select bind:value={selectedProvider} onchange={handleProviderChange} class="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200">
			{#each providers as p}
				<option value={p.provider}>{p.provider}</option>
			{/each}
		</select>
		<select bind:value={selectedModel} class="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 min-w-[200px]">
			{#each availableModels() as model}
				<option value={model}>{model}</option>
			{/each}
			{#if availableModels().length === 0}
				<option value="">{optionsLoading ? 'Loading models…' : loadFailed ? 'Could not load the models' : 'No models available'}</option>
			{/if}
		</select>
		{#if loadFailed}
			<button onclick={load} class="px-2 py-1.5 text-xs rounded bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300">Retry</button>
		{/if}
		<button
			onclick={handleSummarize}
			disabled={!!activeJob || submitting || (!sessionState.activeSession?.transcript?.length && !sessionState.activeSession?.external_notes?.length)}
			class="px-4 py-1.5 text-sm rounded bg-purple-600 hover:bg-purple-700 disabled:bg-gray-700 disabled:text-gray-500 text-white font-medium transition-colors"
		>
			{activeJob ? 'Summarizing...' : sessionState.activeSession?.summary ? 'Re-summarize' : 'Summarize'}
		</button>
	</div>
	{#if sessionState.activeSession?.summary}
		<form onsubmit={(e) => { e.preventDefault(); handleRevise(); }} class="flex flex-wrap items-center gap-2">
			<input
				bind:value={reviseText}
				placeholder="Revise it: shorter, focus on the decisions, say who owns what…"
				aria-label="How to revise the summary"
				class="flex-1 min-w-[16rem] bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 placeholder-gray-500"
			/>
			<button
				type="submit"
				disabled={!reviseText.trim() || !!activeJob || submitting}
				class="px-4 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 border border-purple-700 text-purple-200 disabled:opacity-50"
				title="Rewrite the summary as asked, keeping your edits unless asked otherwise"
			>Revise</button>
		</form>
		{#if summaryMerge && sessionState.activeSession?.summary_data?.edited_at}
			<p class="text-xs text-gray-500">Your edits are kept when the summary is made again.</p>
		{/if}
	{/if}
	{/if}

	{#if sessionState.activeSession?.summary_stale && !activeJob}
		<p class="text-xs text-yellow-500">
			The transcript changed (text or speaker names) since this summary was made. Re-summarize to update it.
		</p>
	{/if}

	{#if activeJob}
		<p class="flex items-center gap-2 text-sm text-gray-400">
			<span class="w-2 h-2 rounded-full bg-purple-500 animate-pulse"></span>
			{activeJob.status === 'queued' ? 'Queued...' : activeJob.message || 'Summarizing...'}
			<span class="text-gray-600">· you can keep working; the summary appears here when ready</span>
		</p>
	{/if}

	{#if error || jobError}
		<p class="text-red-400 text-sm">{error || jobError}</p>
	{/if}

	{#if !sessionState.activeSession?.transcript?.length && !sessionState.activeSession?.external_notes?.length}
		<p class="text-gray-600 text-sm">Record and transcribe audio first to generate a summary, or add notes from another assistant.</p>
	{/if}

	<ExternalNotesCard />

	{#if liveNotes}
		<details class="bg-gray-900 border border-gray-800 rounded-lg p-3 text-sm" open={!sessionState.activeSession?.summary}>
			<summary class="cursor-pointer text-xs font-semibold text-gray-400 uppercase tracking-wide">
				Live notes <span class="normal-case font-normal text-gray-600">· taken by the copilot while recording</span>
			</summary>
			<div class="mt-2 grid gap-3 md:grid-cols-2 text-gray-300">
				{#each [['So far', liveNotes.summary], ['Decided', liveNotes.decisions], ['To do', liveNotes.action_items.map((a) => (a.owner ? `${a.text} · ${a.owner}` : a.text))], ['Open', liveNotes.open_questions]] as [title, items] (title)}
					{#if items.length}
						<section>
							<h5 class="text-[11px] text-gray-500 mb-1">{title}</h5>
							<ul class="space-y-0.5 list-disc list-inside">
								{#each items as item}<li>{item}</li>{/each}
							</ul>
						</section>
					{/if}
				{/each}
			</div>
		</details>
	{/if}
</div>
