// The live-notes documents the app has open: ONE connection per meeting, shared by everything
// that shows or writes the notes (the Notes tab, the recording tab's card, the Copilot's
// "Add to notes"). Ref-counted: `acquireNotes` creates the document state and connects it on
// first use, `releaseNotes` destroys it on the last release.
//
// Two things reach a document from outside its own socket:
// - the relay's `{"type":"reload"}` is handled by NotesDocState itself (it reconnects);
// - the `/ws` bus event `{type:"notes", session_id}` (the relay folded the notes into
//   Session.notes): a meeting that was waiting for its document to arrive by library sync
//   (`waitingForSync`) rejoins to get it. Reloading Session.notes for the stores that read it
//   (search, the legacy editor) is the controller's job (app/controller.svelte.ts), which
//   already owns the bus handlers that refresh the open meeting.
import { NotesDocState, type NotesDocOptions } from '../notes/doc.svelte.js';
import { ensureWasm } from '../notes/wasm.js';
import type { BackendEvent } from '../types/index.js';
import { wsState } from './websocket.svelte.js';

interface Entry {
  state: NotesDocState;
  refs: number;
}

const entries = new Map<string, Entry>();
let stopBus: (() => void) | null = null;

function onBus(raw: Record<string, unknown>): void {
  const msg = raw as unknown as BackendEvent;
  if (msg.type !== 'notes') return;
  const entry = entries.get(msg.session_id);
  if (entry && entry.state.waitingForSync) entry.state.reconnect();
}

/**
 * The shared document state for a meeting; connects on first acquire. Every call must be
 * paired with `releaseNotes`. `opts` only matter for the call that creates it (tests inject a
 * fake WebSocket).
 */
export function acquireNotes(sessionId: string, opts?: NotesDocOptions): NotesDocState {
  const existing = entries.get(sessionId);
  if (existing) {
    existing.refs++;
    return existing.state;
  }
  const entry: Entry = { state: new NotesDocState(opts), refs: 1 };
  entries.set(sessionId, entry);
  if (!stopBus) stopBus = wsState.onMessage(onBus);
  // The CRDT is wasm: load it before the first hello builds a document.
  ensureWasm().then(
    () => {
      if (entries.get(sessionId) === entry) entry.state.connect(sessionId);
    },
    (e) => {
      if (entries.get(sessionId) !== entry) return;
      entry.state.error = e instanceof Error ? e.message : 'the editor could not load';
      entry.state.status = 'error';
    }
  );
  return entry.state;
}

/** Give a state back; the last release closes the connection. Extra releases are ignored. */
export function releaseNotes(sessionId: string): void {
  const entry = entries.get(sessionId);
  if (!entry) return;
  if (--entry.refs > 0) return;
  entries.delete(sessionId);
  entry.state.destroy();
  if (entries.size === 0 && stopBus) {
    stopBus();
    stopBus = null;
  }
}

/** The state if the meeting's notes are open (no reference taken). */
export function peekNotes(sessionId: string): NotesDocState | undefined {
  return entries.get(sessionId)?.state;
}
