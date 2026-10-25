<script lang="ts">
	// The notes editor's fixed toolbar, one slim row (phase 11): "Turn into" (the line kinds),
	// Bold, Italic, Task, Insert (the `/` menu at the caret), Comment, and on the
	// guest page Undo/Redo. Read-only: Comment only, when commenting is allowed. Buttons keep the
	// editor's selection (mousedown is cancelled); the editor does the work through the callbacks.
	import type { LineKind } from '../model.js';
	import type { ToolbarActive } from './types.js';
	import TurnIntoMenu from './TurnIntoMenu.svelte';

	let {
		active,
		readOnly,
		canComment,
		selected,
		commentActive = false,
		hideCommentUntilSelected = false,
		hideComment = false,
		undoButtons = false,
		canUndo = false,
		canRedo = false,
		onkind,
		ontoggle,
		ontask,
		oncomment,
		onundo,
		onredo,
		oninsert
	}: {
		active: ToolbarActive;
		readOnly: boolean;
		canComment: boolean;
		/** Is text selected in the editor? */
		selected: boolean;
		commentActive?: boolean;
		hideCommentUntilSelected?: boolean;
		/** The selection toolbar has its own Comment button while it shows: only one at a time. */
		hideComment?: boolean;
		undoButtons?: boolean;
		canUndo?: boolean;
		canRedo?: boolean;
		/** "Turn into": set the line kind (null = plain text). */
		onkind: (kind: LineKind | null) => void;
		ontoggle: (kind: 'bold' | 'italic') => void;
		/** Toggle the task list. */
		ontask: () => void;
		oncomment: () => void;
		onundo?: () => void;
		onredo?: () => void;
		/** Open the `/` menu at the caret (no handler: the button is disabled). */
		oninsert?: () => void;
	} = $props();

	const GROUP = 'flex rounded border border-gray-700 overflow-hidden text-xs';
	const btn = (on: boolean) =>
		`px-2 py-1 ${on ? 'bg-gray-700 text-gray-100' : 'bg-gray-900 text-gray-400 hover:text-gray-200'}`;

	const marks = [
		{ kind: 'bold', label: 'B', name: 'Bold', keys: 'Ctrl+B', cls: 'font-bold' },
		{ kind: 'italic', label: 'I', name: 'Italic', keys: 'Ctrl+I', cls: 'italic' }
	] as const;

	const showComment = $derived(canComment && !hideComment && (selected || !hideCommentUntilSelected));
</script>

{#if !readOnly || showComment}
	<div class="flex flex-wrap gap-1.5" role="toolbar" aria-label={readOnly ? 'Notes' : 'Format notes'}>
		{#if !readOnly}
			<TurnIntoMenu kind={active.kind} {onkind} />
			<div class={GROUP}>
				{#each marks as m}
					<button
						type="button"
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
					onmousedown={(e) => e.preventDefault()}
					onclick={ontask}
					aria-label="Task list"
					aria-pressed={active.kind === 'task'}
					title="Task list (Ctrl+Shift+9)"
					class={btn(active.kind === 'task')}>☐</button
				>
			</div>
			<div class={GROUP}>
				<button
					type="button"
					onmousedown={(e) => e.preventDefault()}
					onclick={() => oninsert?.()}
					disabled={!oninsert}
					aria-label="Insert"
					aria-haspopup="listbox"
					title="Insert (Ctrl+/)"
					class="{btn(false)} disabled:opacity-40 disabled:hover:text-gray-400">＋ Insert</button
				>
			</div>
			{#if undoButtons}
				<div class={GROUP}>
					<button
						type="button"
						onmousedown={(e) => e.preventDefault()}
						onclick={() => onundo?.()}
						disabled={!canUndo}
						aria-label="Undo"
						title="Undo your last edit (Ctrl+Z)"
						class="{btn(false)} disabled:opacity-40 disabled:hover:text-gray-400">↶</button
					>
					<button
						type="button"
						onmousedown={(e) => e.preventDefault()}
						onclick={() => onredo?.()}
						disabled={!canRedo}
						aria-label="Redo"
						title="Redo (Ctrl+Shift+Z)"
						class="{btn(false)} disabled:opacity-40 disabled:hover:text-gray-400">↷</button
					>
				</div>
			{/if}
		{/if}
		{#if showComment}
			<div class={GROUP}>
				<button
					type="button"
					onmousedown={(e) => e.preventDefault()}
					onclick={oncomment}
					disabled={!selected}
					aria-label="Comment on the selection"
					title={selected ? 'Comment on the selected text (Ctrl+Alt+M)' : 'Select text to comment on (Ctrl+Alt+M)'}
					class="{btn(commentActive)} disabled:opacity-40 disabled:hover:text-gray-400">💬 Comment</button
				>
			</div>
		{/if}
	</div>
{/if}
