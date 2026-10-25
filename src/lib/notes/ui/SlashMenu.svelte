<script lang="ts" module>
	import type { EditorInput } from '../editor/input.js';
	import type { EditorView } from '../editor/view.js';
	import type { Doc } from '../model.js';

	/** What the menu needs from the mounted editor (NotesEditor's `ctx`). */
	export interface SlashEditor {
		doc: Doc;
		view: EditorView;
		input: EditorInput;
		root: HTMLElement;
	}

	let seq = 0;
</script>

<script lang="ts">
	// The `/` menu and the `@` / `[[` pickers (phase 11, task 4): one listbox under the caret, inside
	// the editor's box but outside the contenteditable. The editor keeps focus: the keys come in
	// through `key` (the input layer's interceptKey), typed characters through `typed` (onTyped),
	// and the active option is announced with aria-activedescendant on the editor root. Picking runs
	// ONE editor command (delete the trigger and the query, then apply), so one undo restores the
	// typed text. Pure parts in slash.ts.
	import { tick } from 'svelte';
	import { insertChip, meetingUrl, personUrl, timeUrl } from '../chips.js';
	import { deleteRange, insertTable, setLineKind } from '../commands.js';
	import { lineAt, lines, type LineKind } from '../model.js';
	import type { Inverse } from '../ops.js';
	import type { Rect } from './placement.js';
	import {
		filter,
		GROUP_LABEL,
		idPart,
		menuItems,
		placeMenu,
		regionOf,
		triggerAt,
		triggerOf,
		type MenuItem,
		type MenuSpan
	} from './slash.js';
	import type { InsertProvider } from './types.js';

	let {
		editor,
		box,
		inserts
	}: {
		/** The mounted editor, null while there is none. */
		editor: () => SlashEditor | null;
		/** The editor's box (positioning origin). */
		box: HTMLElement | undefined;
		inserts: InsertProvider | undefined;
	} = $props();

	type Mode = 'menu' | 'person' | 'meeting';

	interface Row {
		key: string;
		label: string;
		hint?: string;
		/** Index in the flat list (the arrows' order). */
		index: number;
		item?: MenuItem;
		person?: { name: string };
		meeting?: { id: string; label: string };
	}

	interface Section {
		label: string;
		rows: Row[];
	}

	const listId = `ne-slash-${++seq}`;
	const PICKER_LABEL = { person: 'People', meeting: 'Earlier meetings' } as const;
	const LIST_LABEL: Record<Mode, string> = { menu: 'Insert', person: 'People', meeting: 'Earlier meetings' };
	const DEBOUNCE_MS = 120;

	let session = $state.raw<{ mode: Mode; span: MenuSpan; line: 'text' | 'code' | 'table' } | null>(null);
	let query = $state('');
	let active = $state(0);
	let timeLabel = $state<string | null>(null);
	let found = $state.raw<Omit<Row, 'index'>[]>([]);
	let loading = $state(false);
	let pos = $state<{ top: number; left: number } | null>(null);
	let listEl = $state<HTMLElement>();
	let fetchSeq = 0;
	let timer: ReturnType<typeof setTimeout> | null = null;

	const sections = $derived.by((): Section[] => {
		const s = session;
		if (!s) return [];
		let i = 0;
		if (s.mode === 'menu') {
			const items = filter(
				menuItems({ time: timeLabel, people: !!inserts?.people, meetings: !!inserts?.meetings, line: s.line }),
				query
			);
			const out: Section[] = [];
			for (const g of ['meeting', 'blocks'] as const) {
				const rows = items
					.filter((it) => it.group === g)
					.map((it) => ({ key: `m-${it.key}`, label: it.name, hint: it.hint, item: it, index: 0 }));
				if (rows.length) out.push({ label: GROUP_LABEL[g], rows });
			}
			for (const sec of out) for (const r of sec.rows) r.index = i++;
			return out;
		}
		const rows = found.map((r) => ({ ...r, index: i++ }));
		return rows.length ? [{ label: PICKER_LABEL[s.mode], rows }] : [];
	});
	const rows = $derived(sections.flatMap((s) => s.rows));
	const optionId = (r: Row) => `${listId}-${r.key}`;

	// The editor root announces the active option while the list is open.
	$effect(() => {
		const s = session;
		const row = rows[active];
		const root = editor()?.root;
		if (!root) return;
		if (!s) {
			clearAria(root);
			return;
		}
		root.setAttribute('aria-controls', listId);
		if (row) {
			root.setAttribute('aria-activedescendant', optionId(row));
			const id = optionId(row);
			void tick().then(() => document.getElementById(id)?.scrollIntoView?.({ block: 'nearest' }));
		} else root.removeAttribute('aria-activedescendant');
	});

	$effect(() => () => {
		if (timer) clearTimeout(timer);
		const root = editor()?.root;
		if (root) clearAria(root);
	});

	function clearAria(root: HTMLElement) {
		root.removeAttribute('aria-activedescendant');
		root.removeAttribute('aria-controls');
	}

	/** The caret line's kind as the menu sees it. */
	function lineType(ed: SlashEditor, caret: number): 'text' | 'code' | 'table' {
		const l = lineAt(lines(ed.doc), caret);
		return l.cells ? 'table' : l.kind === 'code' ? 'code' : 'text';
	}

	const idBefore = (ed: SlashEditor, pos: number): string | null =>
		pos > 0 ? (ed.doc.web.seqIdAt(ed.doc.obj, pos - 1) ?? null) : null;

	function open(mode: Mode, span: MenuSpan, caret: number) {
		const ed = editor();
		if (!ed) return;
		fetchSeq++;
		if (timer) clearTimeout(timer);
		timer = null;
		session = { mode, span, line: lineType(ed, caret) };
		query = '';
		active = 0;
		found = [];
		loading = false;
		timeLabel = inserts?.time?.()?.label ?? null;
		recheck(caret);
		if (session && mode !== 'menu') void fetchNow(query);
		void tick().then(reposition);
	}

	export function isOpen(): boolean {
		return session !== null;
	}

	export function close() {
		if (!session) return;
		session = null;
		query = '';
		found = [];
		loading = false;
		pos = null;
		fetchSeq++;
		if (timer) clearTimeout(timer);
		timer = null;
		const root = editor()?.root;
		if (root) clearAria(root);
	}

	/**
	 * Follow the caret and the document (after a local or remote change, a selection change):
	 * the query is the text between the anchor and the caret; anything else closes the menu.
	 */
	export function recheck(caret?: number) {
		const s = session;
		if (!s) return;
		const ed = editor();
		if (!ed || ed.input.readOnly) return close();
		if (caret === undefined) {
			const sel = ed.view.getSelection();
			if (!sel || sel.anchor !== sel.focus) return close();
			caret = sel.focus;
		}
		const r = regionOf(ed.doc, s.span, caret);
		if (!r) return close();
		if (r.query !== query) {
			query = r.query;
			active = 0;
			if (s.mode !== 'menu') fetchSoon(r.query);
		}
	}

	/** One natively typed character (never during a composition): a trigger opens the menu. */
	export function typed(char: string, caret: number) {
		const ed = editor();
		if (!ed || ed.input.readOnly || ed.input.composing) return;
		if (session) {
			recheck(caret);
			return;
		}
		if (!triggerAt(ed.doc, caret, char)) return;
		const trigger = triggerOf(char)!;
		const mode: Mode = trigger === '/' ? 'menu' : trigger === '@' ? 'person' : 'meeting';
		if (mode === 'person' && !inserts?.people) return;
		if (mode === 'meeting' && !inserts?.meetings) return;
		const id = ed.doc.web.seqIdAt(ed.doc.obj, caret - trigger.length);
		if (!id) return;
		open(mode, { anchor: { id, trigger }, cut: null }, caret);
	}

	/** Ctrl+/ and the Insert button: the menu at the caret, without trigger text. */
	export function openAtCaret() {
		const ed = editor();
		if (!ed || ed.input.readOnly || ed.input.composing) return;
		const sel = ed.view.getSelection();
		if (!sel) return;
		const caret = sel.focus;
		if (sel.anchor !== caret) ed.view.setSelection(caret);
		open('menu', { anchor: { id: idBefore(ed, caret), trigger: '' }, cut: null }, caret);
	}

	/** The editor's keydown, first (interceptKey): true = handled. */
	export function key(e: KeyboardEvent): boolean {
		if (e.isComposing) return false;
		const mod = e.ctrlKey || e.metaKey;
		if (mod && !e.altKey && !e.shiftKey && (e.key === '/' || e.code === 'Slash')) {
			if (session) close();
			else openAtCaret();
			return true;
		}
		if (!session || mod || e.altKey) return false;
		const n = rows.length;
		switch (e.key) {
			case 'ArrowDown':
				if (n) active = (active + 1) % n;
				return true;
			case 'ArrowUp':
				if (n) active = (active - 1 + n) % n;
				return true;
			case 'Enter':
			case 'Tab': {
				const row = rows[active];
				if (e.shiftKey || !row) {
					close();
					return false;
				}
				pick(row);
				return true;
			}
			case 'Escape':
				close();
				e.stopPropagation(); // the menu took it, not a dialog around the editor
				return true;
			case ' ':
				if (!n && !loading) close();
				return false;
			default:
				return false;
		}
	}

	// ---- the pickers' results ------------------------------------------------------------------------
	function fetchSoon(q: string) {
		if (timer) clearTimeout(timer);
		loading = true;
		timer = setTimeout(() => void fetchNow(q), DEBOUNCE_MS);
	}

	async function fetchNow(q: string) {
		if (timer) clearTimeout(timer);
		timer = null;
		const s = session;
		if (!s || s.mode === 'menu') return;
		const n = ++fetchSeq;
		loading = true;
		try {
			let out: Omit<Row, 'index'>[];
			if (s.mode === 'person') {
				const people = (await inserts?.people?.(q)) ?? [];
				out = people.map((p) => ({ key: `p-${idPart(p.name)}`, label: p.name, hint: p.hint, person: { name: p.name } }));
			} else {
				const meetings = (await inserts?.meetings?.(q)) ?? [];
				out = meetings.map((m) => ({ key: `g-${idPart(m.id)}`, label: m.label, meeting: { id: m.id, label: m.label } }));
			}
			if (n !== fetchSeq) return;
			const seen = new Set<string>();
			found = out.filter((r) => !seen.has(r.key) && (seen.add(r.key), true));
			active = 0;
		} catch {
			if (n === fetchSeq) found = [];
		} finally {
			if (n === fetchSeq) {
				loading = false;
				void tick().then(reposition);
			}
		}
	}

	// ---- picking -------------------------------------------------------------------------------------
	function pick(row: Row) {
		const s = session;
		const ed = editor();
		if (!s || !ed) return close();
		const sel = ed.view.getSelection();
		const caret = sel ? sel.focus : null;
		const reg = caret === null ? null : regionOf(ed.doc, s.span, caret);
		if (caret === null || !reg) return close();
		const { from, to } = reg;
		/** One command: the trigger and the query go, then `apply` (its caret). */
		const run = (apply: (inv: Inverse[]) => number) =>
			ed.input.command((d) => {
				const inv: Inverse[] = [...deleteRange(d, from, to).inverse];
				const at = apply(inv);
				return { inverse: inv, caret: at };
			});
		const chip = (text: string, url: string) => {
			close();
			run((inv) => {
				const r = insertChip(ed.doc, from, text, url);
				inv.push(...r.inverse);
				return r.caret;
			});
		};
		if (row.person) return chip('@' + row.person.name, personUrl(row.person.name));
		if (row.meeting) return chip(row.meeting.label, meetingUrl(row.meeting.id));
		const act = row.item?.act;
		if (!act) return close();
		switch (act.kind) {
			case 'line': {
				const kind: LineKind | null = act.line;
				close();
				run((inv) => {
					const line = lineAt(lines(ed.doc), from);
					const same = line.kind === kind || (kind === 'task' && line.kind === 'task_done');
					if (!line.cells && !same) inv.push(...setLineKind(ed.doc, from, from, kind).inverse);
					return from;
				});
				return;
			}
			case 'table':
				close();
				run((inv) => {
					if (lineAt(lines(ed.doc), from).cells) return from; // already in a table: Tab adds rows
					const t = insertTable(ed.doc, from, 2, 2);
					inv.push(...t.inverse);
					return t.caret;
				});
				return;
			case 'time': {
				const t = inserts?.time?.();
				if (!t) return close();
				return chip(t.label, timeUrl(t.part, t.seconds));
			}
			case 'action': {
				close();
				let start = from;
				run((inv) => {
					const line = lineAt(lines(ed.doc), from);
					if (line.kind !== 'task' && line.kind !== 'task_done') inv.push(...setLineKind(ed.doc, from, from, 'task').inverse);
					start = line.start;
					return line.start;
				});
				// The owner goes first: "☐ @Ana ▌".
				if (inserts?.people) open('person', { anchor: { id: idBefore(ed, start), trigger: '' }, cut: null }, start);
				return;
			}
			case 'person':
			case 'meeting': {
				// The same list becomes the picker; its query is typed after what is there, and a
				// pick removes the trigger and the menu's query with it (one command, one undo).
				const cut = from < to ? (ed.doc.web.seqIdAt(ed.doc.obj, from) ?? null) : null;
				open(act.kind, { anchor: { id: idBefore(ed, caret), trigger: '' }, cut }, caret);
				return;
			}
		}
	}

	// ---- where it goes -------------------------------------------------------------------------------
	/** Under the caret's line (above near the bottom), in the box's coordinates. */
	export function reposition() {
		const s = session;
		const ed = editor();
		const b = box;
		if (!s || !ed || !b) return;
		let rect: Rect | null = null;
		const sel = document.getSelection();
		if (sel && sel.rangeCount && sel.focusNode && ed.root.contains(sel.focusNode)) {
			const range = sel.getRangeAt(0);
			const rs = typeof range.getClientRects === 'function' ? [...range.getClientRects()] : [];
			const r0 = rs[rs.length - 1] ?? (typeof range.getBoundingClientRect === 'function' ? range.getBoundingClientRect() : null);
			if (r0 && (r0.width || r0.height || r0.top || r0.left)) rect = { left: r0.left, top: r0.top, right: r0.right, bottom: r0.bottom };
			if (!rect) {
				// A collapsed range on an empty line has no box in some engines: the line's.
				const node = sel.focusNode;
				const el = node instanceof Element ? node : node.parentElement;
				const lr = el?.getBoundingClientRect?.();
				if (lr) rect = { left: lr.left, top: lr.top, right: lr.left, bottom: lr.bottom };
			}
		}
		if (!rect) {
			pos ??= { top: 0, left: 0 };
			return;
		}
		const br = b.getBoundingClientRect();
		const left = br.left + b.clientLeft;
		const top = br.top + b.clientTop;
		// A box that scrolls clips the menu: flip inside it. Otherwise the viewport is the limit.
		const scrolls = b.scrollHeight > b.clientHeight + 1;
		const viewH = typeof window !== 'undefined' ? window.innerHeight : Infinity;
		const p = placeMenu({
			caret: rect,
			box: { left, top, right: left + b.clientWidth, bottom: top + b.clientHeight },
			scrollTop: b.scrollTop,
			scrollLeft: b.scrollLeft,
			menu: { width: listEl?.offsetWidth || 256, height: listEl?.offsetHeight || 200 },
			limitBottom: scrolls ? top + b.clientHeight : viewH,
			limitTop: scrolls ? top : 0
		});
		if (!pos || pos.top !== p.top || pos.left !== p.left) pos = { top: p.top, left: p.left };
	}
</script>

{#if session && pos}
	<!-- The editor keeps focus: mousedown is cancelled so a click picks without blurring it. -->
	<div
		bind:this={listEl}
		id={listId}
		role="listbox"
		tabindex="-1"
		aria-label={LIST_LABEL[session.mode]}
		data-testid="notes-insert-menu"
		onmousedown={(e) => e.preventDefault()}
		style="top: {pos.top}px; left: {pos.left}px"
		class="absolute z-40 w-64 max-w-[calc(100%-0.5rem)] max-h-72 overflow-y-auto rounded border border-gray-700 bg-gray-900 py-1 text-xs shadow-lg"
	>
		{#each sections as sec (sec.label)}
			<div role="group" aria-label={sec.label}>
				<div aria-hidden="true" class="px-3 pt-1.5 pb-0.5 text-[10px] uppercase tracking-wide text-gray-500">{sec.label}</div>
				{#each sec.rows as row (row.key)}
					<!-- svelte-ignore a11y_click_events_have_key_events (the editor's keys drive the list) -->
					<div
						id={optionId(row)}
						role="option"
						tabindex="-1"
						aria-selected={row.index === active}
						onmousemove={() => {
							if (active !== row.index) active = row.index;
						}}
						onclick={() => pick(row)}
						class="flex cursor-pointer items-center gap-2 px-3 py-1 {row.index === active
							? 'bg-gray-700 text-gray-100'
							: 'text-gray-300 hover:bg-gray-800'}"
					>
						<span class="truncate">{row.label}</span>
						{#if row.hint}<span class="ml-auto shrink-0 text-gray-500">{row.hint}</span>{/if}
					</div>
				{/each}
			</div>
		{/each}
		{#if !rows.length}
			<div class="px-3 py-1 text-gray-500">{loading ? 'Searching…' : 'No matches'}</div>
		{/if}
	</div>
{/if}
