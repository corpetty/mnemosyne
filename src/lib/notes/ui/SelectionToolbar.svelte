<script module lang="ts">
	import type { LineKind } from '../model.js';

	export type SelectionMark = 'bold' | 'italic' | 'strike' | 'code';

	/** What the buttons show as on, for the selection's start. */
	export interface SelectionActive {
		kind: LineKind | null;
		bold: boolean;
		italic: boolean;
		strike: boolean;
		code: boolean;
		link: boolean;
	}
</script>

<script lang="ts">
	// The selection toolbar (phase 11, task 3): a small floating `role="toolbar"` that NotesEditor
	// puts above (or below) a non-collapsed selection, as a sibling of the contenteditable inside the
	// editor box, never inside it. Bold, Italic, Strikethrough, Inline code, Link, Comment and
	// "Turn into". Read-only editors get Comment only. Buttons keep the editor's selection (mousedown
	// is cancelled); the editor does the work through the callbacks and decides when to show it.
	// Keyboard: Alt+F10 in the editor focuses the first button (`focusFirst`), the arrow keys, Home
	// and End move between buttons (roving tabindex), Escape calls `onescape`.
	import TurnIntoMenu from './TurnIntoMenu.svelte';

	let {
		active,
		readOnly,
		canComment,
		commentActive = false,
		top,
		left,
		side,
		onkind,
		ontoggle,
		onlink,
		oncomment,
		onescape
	}: {
		active: SelectionActive;
		readOnly: boolean;
		canComment: boolean;
		commentActive?: boolean;
		/** Offsets inside the editor box (position: absolute). */
		top: number;
		left: number;
		side: 'above' | 'below';
		onkind: (kind: LineKind | null) => void;
		ontoggle: (mark: SelectionMark) => void;
		onlink: () => void;
		oncomment: () => void;
		/** Escape: give the editor its focus and selection back. */
		onescape: () => void;
	} = $props();

	const GROUP = 'flex rounded border border-gray-700 overflow-hidden text-xs';
	const btn = (on: boolean) =>
		`px-2 py-1 ${on ? 'bg-gray-700 text-gray-100' : 'bg-gray-900 text-gray-400 hover:text-gray-200'}`;

	const marks = [
		{ kind: 'bold', label: 'B', name: 'Bold', keys: 'Ctrl+B', cls: 'font-bold' },
		{ kind: 'italic', label: 'I', name: 'Italic', keys: 'Ctrl+I', cls: 'italic' },
		{ kind: 'strike', label: 'S', name: 'Strikethrough', keys: 'Ctrl+Shift+X', cls: 'line-through' },
		{ kind: 'code', label: '<>', name: 'Inline code', keys: 'Ctrl+E', cls: 'font-mono' }
	] as const;

	let el = $state<HTMLElement>();
	/** Which button is in the tab order (roving tabindex). */
	let rover = 0;

	const buttons = () => [...(el?.querySelectorAll<HTMLButtonElement>('[data-roving]') ?? [])];

	function sync() {
		buttons().forEach((b, i) => b.setAttribute('tabindex', i === rover ? '0' : '-1'));
	}

	// The set of buttons changes with the mode (read-only: Comment only); keep one in the tab order.
	$effect(() => {
		void readOnly;
		void canComment;
		const n = buttons().length;
		if (rover >= n) rover = 0;
		sync();
	});

	/** Focus the first button (Alt+F10 in the editor). */
	export function focusFirst(): void {
		rover = 0;
		sync();
		buttons()[0]?.focus();
	}

	/** Does the toolbar hold the keyboard focus? */
	export function hasFocus(): boolean {
		return !!el?.contains(document.activeElement);
	}

	export function element(): HTMLElement | undefined {
		return el;
	}

	function onfocusin(e: FocusEvent) {
		const i = buttons().indexOf(e.target as HTMLButtonElement);
		if (i < 0) return;
		rover = i;
		sync();
	}

	function onkeydown(e: KeyboardEvent) {
		// Keys inside the open menu are the menu's own.
		if (e.target instanceof Element && e.target.closest('[role="menu"]')) return;
		const all = buttons();
		const at = all.indexOf(e.target as HTMLButtonElement);
		const move = (i: number) => all[(i + all.length) % all.length]?.focus();
		if (e.key === 'ArrowRight') move(at + 1);
		else if (e.key === 'ArrowLeft') move(at - 1);
		else if (e.key === 'Home') move(0);
		else if (e.key === 'End') move(all.length - 1);
		else if (e.key === 'Escape') onescape();
		else return;
		e.preventDefault();
		e.stopPropagation();
	}
</script>

<!-- A press on the toolbar must not move the editor's selection. -->
<!-- svelte-ignore a11y_no_noninteractive_element_interactions, a11y_interactive_supports_focus -->
<div
	bind:this={el}
	role="toolbar"
	aria-label="Format selection"
	data-side={side}
	data-testid="selection-toolbar"
	{onkeydown}
	{onfocusin}
	onmousedown={(e) => e.preventDefault()}
	style="top:{top}px;left:{left}px"
	class="absolute z-30 flex w-max max-w-full flex-wrap gap-1.5 rounded-md border border-gray-700 bg-gray-950 p-1 shadow-lg"
>
	{#if !readOnly}
		<TurnIntoMenu kind={active.kind} {onkind} up={side === 'above'} />
		<div class={GROUP}>
			{#each marks as m}
				<button
					type="button"
					data-roving
					onmousedown={(e) => e.preventDefault()}
					onclick={() => ontoggle(m.kind)}
					aria-label={m.name}
					aria-pressed={active[m.kind]}
					title="{m.name} ({m.keys})"
					class="{btn(active[m.kind])} {m.cls}">{m.label}</button
				>
			{/each}
			<button
				type="button"
				data-roving
				onmousedown={(e) => e.preventDefault()}
				onclick={onlink}
				aria-label="Link"
				aria-pressed={active.link}
				title="Link (Ctrl+K)"
				class={btn(active.link)}>🔗</button
			>
		</div>
	{/if}
	{#if canComment}
		<div class={GROUP}>
			<button
				type="button"
				data-roving
				onmousedown={(e) => e.preventDefault()}
				onclick={oncomment}
				aria-label="Comment on the selection"
				title="Comment on the selected text (Ctrl+Alt+M)"
				class={btn(commentActive)}>💬 Comment</button
			>
		</div>
	{/if}
</div>
