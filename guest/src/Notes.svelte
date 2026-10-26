<script lang="ts">
	// The guest page's notes: the app's shared notes editor (src/lib/notes/ui/NotesEditor.svelte) on
	// a GuestDoc, the same view, input layer, local undo and remote handling as the app's
	// SharedNotes.svelte. No app stores: messages show in a line under the toolbar instead of the
	// app's toasts, and the toolbar keeps Undo/Redo (phones have no Ctrl+Z).
	import NotesEditor from '$lib/notes/ui/NotesEditor.svelte';
	import type { NotesHost } from '$lib/notes/ui/types.js';
	import { createOp, mintId, type Anchor } from './comments';
	import type { GuestDoc } from './doc';
	import { createGuestInserts } from './inserts';
	import { createGuestPresence } from './presence';

	let {
		guest,
		rev,
		locked = false,
		openIds = new Set<string>(),
		cancomment = false,
		oncompose,
		onpost,
		onmark
	}: {
		guest: GuestDoc;
		/** Changes whenever `guest` notified a change (GuestDoc is not reactive itself). */
		rev: number;
		/** Read-only until the guest has a name (the first visit's name card). */
		locked?: boolean;
		/** Ids of the threads that are open: their marks are highlighted. */
		openIds?: ReadonlySet<string>;
		/** May the selection be commented on? Shows the Comment button and answers Ctrl+Alt+M. */
		cancomment?: boolean;
		/** The selection to comment on (its text and the ids of its first and last character). */
		oncompose?: (anchor: Anchor) => void;
		/**
		 * Post a comment on a selection now (the touch screens' comment sheet). Without it the
		 * comment goes straight to the room (sendComment); the comments panel shows it once the
		 * owner's server has it.
		 */
		onpost?: (anchor: Anchor, text: string) => Promise<void>;
		/** A click on a highlighted mark: the open threads on it, first one first. */
		onmark?: (ids: string[]) => void;
	} = $props();

	const doc = $derived.by(() => {
		void rev;
		return guest.doc;
	});
	const readOnly = $derived.by(() => {
		void rev;
		const s = guest.status;
		return locked || guest.readOnly || s === 'ended' || s === 'gone';
	});
	const presence = $derived(createGuestPresence(guest));
	const inserts = $derived(createGuestInserts(guest));

	let editor = $state<ReturnType<typeof NotesEditor>>();

	/** The GuestDoc the mounted editor is bound to (its remote hooks are on it). */
	let bound: GuestDoc | null = null;
	const target = () => bound ?? guest;

	const host: NotesHost = {
		get doc() {
			return doc;
		},
		get readOnly() {
			return readOnly;
		},
		get canComment() {
			return cancomment;
		},
		hideCommentUntilSelected: true,
		undoButtons: true,
		get openComments() {
			return openIds;
		},
		localChange: () => target().localChange(),
		setTyping: (on) => target().setTyping(on),
		flush: () => target().flush(),
		bindRemote(hooks) {
			const g = guest;
			bound = g;
			g.wrapRemote = hooks.wrap;
			g.onRemoteChange = hooks.after;
			return () => {
				if (g.wrapRemote === hooks.wrap) g.wrapRemote = undefined;
				if (g.onRemoteChange === hooks.after) g.onRemoteChange = undefined;
				if (bound === g) bound = null;
			};
		},
		compose: (sel) => oncompose?.({ text: sel.text, first: sel.first, last: sel.last }),
		postComment: (sel, text) => post({ text: sel.text, first: sel.first, last: sel.last }, text),
		onMark: (ids) => onmark?.(ids),
		say,
		get presence() {
			return presence;
		},
		get inserts() {
			return inserts;
		}
	};

	/** A new thread on `anchor`, as the comments panel sends one; rejects when it did not go out. */
	async function post(anchor: Anchor, text: string): Promise<void> {
		if (onpost) return onpost(anchor, text);
		const g = target();
		// The bridge marks the characters from their ids, so it must have them: what was typed last goes first.
		await g.synced();
		if (!(await g.sendComment(createOp(mintId(), text, anchor))))
			throw new Error('Not connected: the comment was not sent');
	}

	/** Scroll to comment `id`'s mark and flash it. False when no character carries it. */
	export function goTo(id: string): boolean {
		return editor?.flashComment(id) ?? false;
	}

	// ---- messages (the app shows these as toasts) --------------------------------------------------
	let notice = $state('');
	let noticeTimer: ReturnType<typeof setTimeout> | null = null;
	function say(text: string) {
		notice = text;
		if (noticeTimer) clearTimeout(noticeTimer);
		noticeTimer = setTimeout(() => (notice = ''), 5000);
	}
	$effect(() => () => {
		if (noticeTimer) clearTimeout(noticeTimer);
	});
</script>

<NotesEditor bind:this={editor} {host} testid="guest-notes-editor" label="Meeting notes">
	{#snippet under()}
		<p class="min-h-4 text-[11px] text-amber-400" aria-live="polite">{notice}</p>
	{/snippet}
</NotesEditor>
