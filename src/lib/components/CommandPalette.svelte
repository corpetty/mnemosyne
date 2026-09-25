<script lang="ts" module>
	import type { PersonSummary, TaskItem, TopicCount } from '$lib/types/index.js';

	// Fetched on first open and kept for the rest of the session (refreshed on each open).
	const cache: { people: PersonSummary[]; topics: TopicCount[]; tasks: TaskItem[] } = {
		people: [],
		topics: [],
		tasks: []
	};
	const RECENT_KEY = 'mnemosyne.palette.recent';
</script>

<script lang="ts">
	import { listActionItems, listPeople, listTopics } from '$lib/api/backend.js';
	import { exportActive, openSetup, openView, startRecording, stopAndTranscribe } from '$lib/app/controller.svelte.js';
	import { fuzzyScore } from '$lib/app/fuzzy.js';
	import { askState } from '$lib/stores/ask.svelte.js';
	import { audioState } from '$lib/stores/audio.svelte.js';
	import { sessionState } from '$lib/stores/session.svelte.js';
	import { uiState, type SettingsTab, type View } from '$lib/stores/ui.svelte.js';

	interface Item {
		id: string;
		group: string;
		label: string;
		hint?: string;
		run: () => unknown;
	}

	let query = $state('');
	let active = $state(0);
	let input = $state<HTMLInputElement>();
	let list = $state<HTMLUListElement>();
	let people = $state(cache.people);
	let topics = $state(cache.topics);
	let tasks = $state(cache.tasks);
	const returnFocus = document.activeElement as HTMLElement | null;

	$effect(() => {
		input?.focus();
		listPeople().then((p) => (people = cache.people = p)).catch(() => {});
		listTopics().then((t) => (topics = cache.topics = t)).catch(() => {});
		listActionItems('open').then((t) => (tasks = cache.tasks = t)).catch(() => {});
		return () => returnFocus?.focus?.();
	});

	function readRecent(): string[] {
		try {
			return JSON.parse(localStorage.getItem(RECENT_KEY) ?? '[]');
		} catch {
			return [];
		}
	}

	function remember(id: string) {
		try {
			const ids = [id, ...readRecent().filter((x) => x !== id)].slice(0, 6);
			localStorage.setItem(RECENT_KEY, JSON.stringify(ids));
		} catch {
			// private window or blocked storage: no recents
		}
	}

	function view(v: View, label: string): Item {
		return { id: `view:${v}`, group: 'Go to', label, run: () => openView(v) };
	}

	function settings(tab: SettingsTab, label: string): Item {
		return {
			id: `settings:${tab}`,
			group: 'Go to',
			label: `Settings · ${label}`,
			run: () => {
				uiState.settingsTab = tab;
				openView('settings');
			}
		};
	}

	function openMeeting(id: string, tab?: 'summary') {
		void sessionState.selectSession(id).then(() => {
			if (tab) uiState.activeTab = tab;
		});
	}

	const day = (iso: string) => new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });

	const items = $derived.by((): Item[] => {
		const actions: Item[] = [
			audioState.isRecording
				? { id: 'action:stop', group: 'Actions', label: 'Stop and transcribe', hint: 'Ctrl+S', run: () => stopAndTranscribe() }
				: { id: 'action:record', group: 'Actions', label: 'Start recording', hint: 'Ctrl+R', run: () => startRecording(true) },
			{ id: 'action:import', group: 'Actions', label: 'Import an audio or video file…', run: () => uiState.importRequest++ },
			{ id: 'action:sidebar', group: 'Actions', label: 'Show or hide the sidebar', hint: 'Ctrl+B', run: () => (uiState.sidebarCollapsed = !uiState.sidebarCollapsed) },
			{ id: 'action:setup', group: 'Actions', label: 'Run setup again', run: () => openSetup() }
		];
		if (sessionState.activeSession) {
			actions.push({ id: 'action:export', group: 'Actions', label: 'Export this meeting to Obsidian', hint: 'Ctrl+E', run: () => exportActive() });
		}
		const views: Item[] = [
			view('home', 'Home'),
			view('ask', 'Ask your meetings'),
			view('tasks', 'Tasks'),
			view('people', 'People'),
			view('topics', 'Topics'),
			view('digest', 'Weekly digest'),
			settings('general', 'General'),
			settings('recording', 'Recording'),
			settings('transcription', 'Transcription'),
			settings('ai', 'AI'),
			settings('notes', 'Notes & sharing')
		];
		const meetings: Item[] = sessionState.sessions.map((s) => ({
			id: `meeting:${s.id}`,
			group: 'Meetings',
			label: s.name,
			hint: day(s.created_at),
			run: () => openMeeting(s.id)
		}));
		const persons: Item[] = people.map((p) => ({
			id: `person:${p.name}`,
			group: 'People',
			label: p.name,
			hint: `${p.meetings} meeting${p.meetings === 1 ? '' : 's'}`,
			run: () => {
				uiState.personRequest = p.name;
				openView('people');
			}
		}));
		const topicItems: Item[] = topics.map((t) => ({
			id: `topic:${t.topic}`,
			group: 'Topics',
			label: t.topic,
			hint: `${t.meetings} meeting${t.meetings === 1 ? '' : 's'}`,
			run: () => {
				uiState.topicRequest = t.topic;
				openView('topics');
			}
		}));
		const taskItems: Item[] = tasks.map((t) => ({
			id: `task:${t.session_id}:${t.idx}`,
			group: 'Open tasks',
			label: t.text,
			hint: [t.owner, t.session_name].filter(Boolean).join(' · '),
			run: () => openMeeting(t.session_id, 'summary')
		}));
		return [...actions, ...views, ...meetings, ...persons, ...topicItems, ...taskItems];
	});

	const results = $derived.by((): Item[] => {
		const q = query.trim();
		if (!q) {
			const byId = new Map(items.map((i) => [i.id, i]));
			const recent = readRecent()
				.map((id) => byId.get(id))
				.filter((i): i is Item => !!i)
				.map((i) => ({ ...i, group: 'Recent' }));
			const seen = new Set(recent.map((i) => i.id));
			const rest = items.filter((i) => !seen.has(i.id) && i.group !== 'People' && i.group !== 'Topics' && i.group !== 'Open tasks');
			return [...recent, ...rest.filter((i) => i.group !== 'Meetings'), ...rest.filter((i) => i.group === 'Meetings').slice(0, 8)];
		}
		const ranked = items
			.map((item, order) => ({ item, order, score: fuzzyScore(q, item.label) }))
			.filter((r) => r.score !== null)
			.sort((a, b) => b.score! - a.score! || a.order - b.order)
			.slice(0, 50)
			.map((r) => r.item);
		const question = q.replace(/^ask\s+/i, '');
		const ask: Item = {
			id: 'ask-question',
			group: 'Ask',
			label: `Ask your meetings: “${question}”`,
			run: () => {
				openView('ask');
				void askState.ask(question);
			}
		};
		return /^ask\s+\S/i.test(q) ? [ask, ...ranked] : [...ranked, ask];
	});

	$effect(() => {
		void query;
		active = 0;
	});

	$effect(() => {
		list?.querySelector(`[data-index="${active}"]`)?.scrollIntoView({ block: 'nearest' });
	});

	function close() {
		uiState.paletteOpen = false;
	}

	function choose(item: Item | undefined) {
		if (!item) return;
		if (item.id !== 'ask-question') remember(item.id);
		close();
		item.run();
	}

	function onkeydown(e: KeyboardEvent) {
		if (e.key === 'ArrowDown') {
			e.preventDefault();
			active = Math.min(active + 1, results.length - 1);
		} else if (e.key === 'ArrowUp') {
			e.preventDefault();
			active = Math.max(active - 1, 0);
		} else if (e.key === 'Enter') {
			e.preventDefault();
			choose(results[active]);
		} else if (e.key === 'Escape') {
			e.preventDefault();
			close();
		}
	}
</script>

<div class="fixed inset-0 z-40 flex items-start justify-center bg-black/50 px-4 pt-[12vh]" role="presentation" onclick={close}>
	<div
		class="w-full max-w-xl rounded-lg border border-gray-700 bg-gray-900 shadow-2xl overflow-hidden"
		role="dialog"
		aria-modal="true"
		aria-label="Command palette"
		tabindex="-1"
		onclick={(e) => e.stopPropagation()}
		onkeydown={(e) => {
			if (!(e.ctrlKey && e.key === 'k')) e.stopPropagation(); // Ctrl+K closes it again
		}}
	>
		<input
			bind:this={input}
			bind:value={query}
			{onkeydown}
			placeholder="Go to a meeting, person, topic or setting, or type a question…"
			aria-label="Search commands"
			role="combobox"
			aria-expanded="true"
			aria-controls="palette-results"
			aria-activedescendant={results.length ? `palette-item-${active}` : undefined}
			class="w-full bg-transparent px-4 py-3 text-sm text-gray-100 placeholder-gray-500 border-b border-gray-800 focus:outline-none"
		/>
		<ul bind:this={list} id="palette-results" role="listbox" class="max-h-[50vh] overflow-y-auto py-1">
			{#each results as item, i (item.id + item.group)}
				{#if i === 0 || results[i - 1].group !== item.group}
					<li role="presentation" class="px-4 pt-2 pb-1 text-[10px] uppercase tracking-wide text-gray-500">{item.group}</li>
				{/if}
				<!-- svelte-ignore a11y_click_events_have_key_events (the keyboard drives the list from the input, as in any combobox) -->
				<li
					id="palette-item-{i}"
					data-index={i}
					role="option"
					aria-selected={i === active}
					class="mx-1 flex cursor-pointer items-center gap-3 rounded px-3 py-1.5 text-sm {i === active ? 'bg-blue-600/30 text-white' : 'text-gray-300'}"
					onmousemove={() => (active = i)}
					onclick={() => choose(item)}
				>
					<span class="truncate">{item.label}</span>
					{#if item.hint}<span class="ml-auto shrink-0 text-xs text-gray-500">{item.hint}</span>{/if}
				</li>
			{/each}
		</ul>
	</div>
</div>
