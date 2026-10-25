import * as backend from '$lib/api/backend.js';
import type { GuestRoom } from '$lib/types/index.js';
import { connectionState } from './connection.svelte.js';

/** A meeting's guest notes room (the link guests edit the notes through), per meeting. */
class GuestNotesState {
  /** null = loaded, no room; absent = not loaded. */
  rooms = $state<Record<string, GuestRoom | null>>({});
  busy = $state(false);
  error = $state<string | null>(null);
  allowGuestNotes = $state(false);
  settingsLoaded = $state(false);

  /** Guest notes are on: always on the desktop, on a team server once an admin allows it. */
  get allowed(): boolean {
    return !connectionState.me?.team_mode || this.allowGuestNotes;
  }

  async loadSettings() {
    try {
      const v = (await backend.getSettings()).values;
      this.allowGuestNotes = !!v.allow_guest_notes;
      this.settingsLoaded = true;
    } catch {
      /* not connected yet: tried again on the next open */
    }
  }

  /** Whether a room has been loaded for this meeting (the controller reloads those on events). */
  watching(sessionId: string): boolean {
    return sessionId in this.rooms;
  }

  async load(sessionId: string): Promise<GuestRoom | null> {
    try {
      const room = await backend.getGuestNotes(sessionId);
      this.rooms[sessionId] = room;
      return room;
    } catch (e) {
      this.error = e instanceof Error ? e.message : String(e);
      return this.rooms[sessionId] ?? null;
    }
  }

  start(sessionId: string): Promise<GuestRoom | null> {
    return this.act(sessionId, backend.startGuestNotes);
  }
  end(sessionId: string): Promise<GuestRoom | null> {
    return this.act(sessionId, backend.endGuestNotes);
  }
  stop(sessionId: string): Promise<GuestRoom | null> {
    return this.act(sessionId, backend.stopGuestNotes);
  }
  newLink(sessionId: string): Promise<GuestRoom | null> {
    return this.act(sessionId, backend.newGuestLink);
  }

  /** Run a change; on failure `error` holds the backend's message and the room is kept. */
  private async act(sessionId: string, call: (id: string) => Promise<GuestRoom | null>): Promise<GuestRoom | null> {
    this.busy = true;
    this.error = null;
    try {
      const room = await call(sessionId);
      this.rooms[sessionId] = room;
      return room;
    } catch (e) {
      this.error = e instanceof Error ? e.message : String(e);
      return this.rooms[sessionId] ?? null;
    } finally {
      this.busy = false;
    }
  }
}

export const guestNotesState = new GuestNotesState();
