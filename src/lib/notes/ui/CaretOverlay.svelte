<script lang="ts">
	// Other people's carets and selections over the notes editor (phase 11, task 14). A layer of
	// zero size at the top-left of the editor's box (which is `position: relative` and may scroll):
	// positioned children scroll with the text, so every rectangle is measured from the layer's own
	// corner and needs no scroll adjustment. Per person: a 2 px bar in their colour, a name flag
	// that shows when they move and fades after 3 s, and their selection as translucent rectangles.
	// Everything is recomputed in ONE animation frame after a presence callback, a change of the
	// document's DOM (a remote frame, a local render, a re-wrap) or a resize.
	import { onMount } from 'svelte';
	import { caretRect, rangeRects, type Rect } from '../editor/rects.js';
	import type { Doc } from '../model.js';
	import { caretOffsets, colorsFor } from './carets.js';
	import type { PresenceSource } from './types.js';

	let {
		presence,
		doc,
		root
	}: {
		presence?: PresenceSource;
		/** The document the editor shows (null while it loads). */
		doc?: Doc | null;
		/** The editor's contenteditable root: its rendered lines are what the rectangles measure. */
		root?: HTMLElement;
	} = $props();

	/** How long a person's name flag stays after they moved (ms). */
	const FLAG_MS = 3000;

	interface Drawn {
		key: string;
		name: string;
		bar: string;
		text: string;
		fill: string;
		/** The caret bar (x, y, h), null when it cannot be measured. */
		caret: Rect | null;
		/** The flag goes under the bar when there is no room above it. */
		below: boolean;
		selection: Rect[];
	}

	let layer = $state<HTMLElement>();
	let drawn = $state.raw<Drawn[]>([]);
	/** Keys whose name flag is showing. */
	let flagged = $state.raw<ReadonlySet<string>>(new Set());

	/** Where each person's selection was last drawn ("a|f"), to tell a move from a re-render. */
	const lastSel = new Map<string, string>();
	const flagTimers = new Map<string, ReturnType<typeof setTimeout>>();
	let frame = 0;

	function showFlag(key: string) {
		const t = flagTimers.get(key);
		if (t) clearTimeout(t);
		if (!flagged.has(key)) flagged = new Set(flagged).add(key);
		flagTimers.set(
			key,
			setTimeout(() => {
				flagTimers.delete(key);
				if (!flagged.has(key)) return;
				const next = new Set(flagged);
				next.delete(key);
				flagged = next;
			}, FLAG_MS)
		);
	}

	function compute() {
		frame = 0;
		const el = layer;
		if (!el || !presence || !doc || !root) {
			if (drawn.length) drawn = [];
			return;
		}
		const box = el.getBoundingClientRect();
		const origin = { left: box.left, top: box.top };
		const out: Drawn[] = [];
		const seen = new Set<string>();
		for (const c of presence.carets()) {
			const at = caretOffsets(doc, c.sel);
			if (!at) continue; // an unknown or deleted anchor: hidden until they move
			const caret = caretRect(root, at.focus, origin);
			if (!caret) continue;
			seen.add(c.key);
			const sig = `${c.sel.a ?? ''}|${c.sel.f ?? ''}`;
			if (lastSel.get(c.key) !== sig) {
				lastSel.set(c.key, sig);
				showFlag(c.key);
			}
			const colors = colorsFor(c.who);
			out.push({
				key: c.key,
				name: c.name,
				bar: colors.bar,
				text: colors.text,
				fill: colors.bar,
				caret,
				below: caret.y < 18,
				selection: at.anchor === at.focus ? [] : rangeRects(root, at.anchor, at.focus, origin)
			});
		}
		// Someone who left, or lost their caret, is forgotten: their next position shows the flag.
		for (const k of [...lastSel.keys()]) if (!seen.has(k)) lastSel.delete(k);
		drawn = out;
	}

	function schedule() {
		if (!frame) frame = requestAnimationFrame(compute);
	}

	// Presence moved or the editor/document changed identity: draw again.
	$effect(() => {
		const p = presence;
		void doc;
		void root;
		void layer;
		schedule();
		return p?.subscribe(schedule);
	});

	// The text changed under the carets (anything the editor renders, local or remote), or the
	// editor's width did (wrapping moves every position).
	$effect(() => {
		const r = root;
		if (!r) return;
		const mo = new MutationObserver(schedule);
		mo.observe(r, { childList: true, subtree: true, characterData: true });
		const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(schedule) : null;
		ro?.observe(r);
		return () => {
			mo.disconnect();
			ro?.disconnect();
		};
	});

	onMount(() => {
		window.addEventListener('resize', schedule);
		return () => {
			window.removeEventListener('resize', schedule);
			if (frame) cancelAnimationFrame(frame);
			for (const t of flagTimers.values()) clearTimeout(t);
			flagTimers.clear();
		};
	});
</script>

<div bind:this={layer} class="carets" aria-hidden="true" data-testid="caret-layer">
	{#each drawn as c (c.key)}
		<div class="person" data-testid="remote-caret" data-name={c.name} data-key={c.key}>
			{#if c.selection.length}
				<div class="selection" style:--c={c.fill}>
					{#each c.selection as r}
						<span style:left="{r.x}px" style:top="{r.y}px" style:width="{r.w}px" style:height="{r.h}px"></span>
					{/each}
				</div>
			{/if}
			{#if c.caret}
				<div
					class="bar"
					style:left="{c.caret.x - 1}px"
					style:top="{c.caret.y}px"
					style:height="{c.caret.h}px"
					style:background={c.bar}
				>
					<span
						class="flag"
						class:shown={flagged.has(c.key)}
						class:below={c.below}
						style:background={c.bar}
						style:color={c.text}>{c.name}</span
					>
				</div>
			{/if}
		</div>
	{/each}
</div>

<style>
	.carets {
		position: absolute;
		left: 0;
		top: 0;
		width: 0;
		height: 0;
		pointer-events: none;
		user-select: none;
	}
	.person {
		position: absolute;
		left: 0;
		top: 0;
	}
	/* One group per person, so overlapping fragments do not add up: solid fill, group opacity. */
	.selection {
		position: absolute;
		left: 0;
		top: 0;
		opacity: 0.28;
	}
	.selection span {
		position: absolute;
		background: var(--c);
	}
	.bar {
		position: absolute;
		width: 2px;
		border-radius: 1px;
	}
	.flag {
		position: absolute;
		left: -1px;
		bottom: 100%;
		max-width: 12rem;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		padding: 0 5px;
		border-radius: 3px 3px 3px 0;
		font-size: 10px;
		line-height: 15px;
		font-weight: 600;
		opacity: 0;
		transition: opacity 0.4s ease;
	}
	.flag.below {
		bottom: auto;
		top: 100%;
		border-radius: 0 3px 3px 3px;
	}
	.flag.shown {
		opacity: 1;
		transition-duration: 0.1s;
	}
	@media (prefers-reduced-motion: reduce) {
		.flag {
			transition: none;
		}
	}
</style>
