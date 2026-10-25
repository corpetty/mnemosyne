<script lang="ts">
	// The "Turn into" menu button shared by the fixed toolbar and the selection toolbar (phase 11):
	// a button that opens a menu of the line kinds. The trigger keeps the editor's selection
	// (mousedown is cancelled); the page does the work through `onkind`.
	import { tick } from 'svelte';
	import type { LineKind } from '../model.js';

	let {
		kind,
		onkind,
		up = false
	}: {
		/** The caret's line kind (null = plain text). */
		kind: LineKind | null;
		/** Set the line kind (null = plain text). */
		onkind: (kind: LineKind | null) => void;
		/** Open the menu upward (the selection toolbar above the text), not downward. */
		up?: boolean;
	} = $props();

	const GROUP = 'flex rounded border border-gray-700 overflow-hidden text-xs';
	const btn = (on: boolean) =>
		`px-2 py-1 ${on ? 'bg-gray-700 text-gray-100' : 'bg-gray-900 text-gray-400 hover:text-gray-200'}`;

	const kinds: { kind: LineKind | null; name: string; keys: string }[] = [
		{ kind: null, name: 'Text', keys: 'Ctrl+Alt+0' },
		{ kind: 'h1', name: 'Heading 1', keys: 'Ctrl+Alt+1' },
		{ kind: 'h2', name: 'Heading 2', keys: 'Ctrl+Alt+2' },
		{ kind: 'h3', name: 'Heading 3', keys: 'Ctrl+Alt+3' },
		{ kind: 'bullet', name: 'Bulleted list', keys: 'Ctrl+Shift+8' },
		{ kind: 'number', name: 'Numbered list', keys: 'Ctrl+Shift+7' },
		{ kind: 'task', name: 'Task list', keys: 'Ctrl+Shift+9' },
		{ kind: 'quote', name: 'Quote', keys: 'type > and a space' },
		{ kind: 'code', name: 'Code block', keys: 'type ``` and Enter' }
	];

	/** The kind the menu shows as current (a line kind outside the menu, e.g. a table, shows Text). */
	const current = $derived(kinds.find((k) => k.kind === kind) ?? kinds[0]);

	let open = $state(false);
	let wrapEl = $state<HTMLElement>();
	let triggerEl = $state<HTMLButtonElement>();
	let menuEl = $state<HTMLElement>();

	const items = () => [...(menuEl?.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]') ?? [])];

	/** Open the menu; from the keyboard (no pointer) the current item takes the focus. */
	async function openMenu(focusItem: boolean) {
		open = true;
		if (!focusItem) return;
		await tick();
		const all = items();
		(all.find((b) => b.getAttribute('aria-checked') === 'true') ?? all[0])?.focus();
	}

	function closeMenu(refocus: boolean) {
		const hadFocus = !!menuEl?.contains(document.activeElement);
		open = false;
		if (refocus && hadFocus) triggerEl?.focus();
	}

	function pick(k: LineKind | null) {
		closeMenu(true);
		onkind(k);
	}

	function onMenuKey(e: KeyboardEvent) {
		const all = items();
		const at = all.indexOf(document.activeElement as HTMLButtonElement);
		const move = (i: number) => all[(i + all.length) % all.length]?.focus();
		if (e.key === 'ArrowDown') move(at + 1);
		else if (e.key === 'ArrowUp') move(at - 1);
		else if (e.key === 'Home') move(0);
		else if (e.key === 'End') move(all.length - 1);
		else if (e.key === 'Escape') closeMenu(true);
		else if (e.key === 'Tab') closeMenu(false);
		else return;
		e.preventDefault();
		e.stopPropagation();
	}

	// A press anywhere else closes the menu.
	$effect(() => {
		if (!open) return;
		const onDown = (e: PointerEvent) => {
			if (wrapEl && e.target instanceof Node && wrapEl.contains(e.target)) return;
			closeMenu(false);
		};
		document.addEventListener('pointerdown', onDown, true);
		return () => document.removeEventListener('pointerdown', onDown, true);
	});
</script>

<div class="relative" bind:this={wrapEl}>
	<div class={GROUP}>
		<button
			bind:this={triggerEl}
			type="button"
			data-roving
			onmousedown={(e) => e.preventDefault()}
			onclick={(e) => (open ? closeMenu(false) : openMenu(e.detail === 0))}
			onkeydown={(e) => {
				if (e.key === 'ArrowDown' && !open) {
					e.preventDefault();
					void openMenu(true);
				}
			}}
			aria-label="Turn into"
			aria-haspopup="menu"
			aria-expanded={open}
			title="Turn into: {current.name}"
			class={btn(open)}>{current.name} ▾</button
		>
	</div>
	{#if open}
		<div
			bind:this={menuEl}
			role="menu"
			tabindex="-1"
			aria-label="Turn into"
			onkeydown={onMenuKey}
			class="absolute left-0 z-40 min-w-40 {up ? 'bottom-full mb-1' : 'top-full mt-1'} rounded border border-gray-700 bg-gray-900 py-1 text-xs shadow-lg"
		>
			{#each kinds as k}
				<button
					type="button"
					role="menuitemradio"
					aria-checked={k.kind === current.kind}
					onmousedown={(e) => e.preventDefault()}
					onclick={() => pick(k.kind)}
					title="{k.name} ({k.keys})"
					class="block w-full px-3 py-1 text-left {k.kind === current.kind
						? 'bg-gray-700 text-gray-100'
						: 'text-gray-300 hover:bg-gray-800 hover:text-gray-100'}">{k.name}</button
				>
			{/each}
		</div>
	{/if}
</div>
