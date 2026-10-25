// What the desktop's notes editor can insert (phase 11, task 19): the moment in the recording,
// people, earlier meetings, and what a click on a chip does. The editor (src/lib/notes/ui) only
// sees the InsertProvider; the pure parts here (the clock maths, the ranking) are unit-tested.
//
// Time: while this meeting records, a chip is (part, seconds since that part's capture started) from
// the backend's part clock; otherwise it is the player's position, put on a part with the meeting's
// recordings (`Recording.part` / `offset`). Labels are on the joined timeline (`offset + seconds`),
// the one the transcript and the player use.
import { formatClock, type Chip } from '$lib/notes/chips.js';
import type { InsertProvider } from '$lib/notes/ui/types.js';
import type { PersonSummary, Recording, SessionSummary } from '$lib/types/index.js';
import { listPeople } from '$lib/api/backend.js';
import { meetingPeople } from '$lib/app/publishing.js';
import { audioState, type PartClock } from '$lib/stores/audio.svelte.js';
import { playerState } from '$lib/stores/player.svelte.js';
import { sessionState } from '$lib/stores/session.svelte.js';
import { toastState } from '$lib/stores/toast.svelte.js';
import { uiState } from '$lib/stores/ui.svelte.js';

const LIMIT = 8;

export interface TimeInsert {
  part: number;
  seconds: number;
  label: string;
}

/** The moment now in a recording whose part clock is `clock`: whole seconds into the part (never
 * negative, whatever the clocks say), labelled on the joined timeline. Null for a bad clock. */
export function recordingTime(clock: PartClock, nowMs: number): TimeInsert | null {
  const started = Date.parse(clock.started_at);
  if (!Number.isFinite(started) || !Number.isFinite(nowMs)) return null;
  const elapsed = Math.max(0, (nowMs - started) / 1000);
  const offset = Number.isFinite(clock.offset) ? Math.max(0, clock.offset) : 0;
  return { part: clock.part, seconds: Math.floor(elapsed), label: formatClock(offset + elapsed) };
}

/** Where each part starts on the joined timeline, by part number (a part's mic and system files
 * share one offset; the smallest one is taken if they ever differ). Sorted by part. */
export function partOffsets(recordings: readonly Pick<Recording, 'part' | 'offset'>[]): { part: number; offset: number }[] {
  const byPart = new Map<number, number>();
  for (const r of recordings) {
    const offset = Number.isFinite(r.offset) ? Math.max(0, r.offset) : 0;
    const known = byPart.get(r.part);
    byPart.set(r.part, known === undefined ? offset : Math.min(known, offset));
  }
  return [...byPart].map(([part, offset]) => ({ part, offset })).sort((a, b) => a.part - b.part);
}

/** A position on the joined timeline as (part, seconds into it): the last part that starts at or
 * before it. A meeting with no recordings counts as one part from 0. Null for a bad position. */
export function locate(position: number, recordings: readonly Pick<Recording, 'part' | 'offset'>[]): TimeInsert | null {
  if (!Number.isFinite(position)) return null;
  const at = Math.max(0, position);
  const parts = partOffsets(recordings);
  let chosen = { part: 0, offset: 0 };
  for (const p of parts) {
    if (p.offset <= at) chosen = p;
  }
  return { part: chosen.part, seconds: Math.floor(at - chosen.offset), label: formatClock(at) };
}

/** Where a time chip points on the joined timeline, or null when the meeting has no such part. */
export function seekTarget(chip: { part: number; seconds: number }, recordings: readonly Pick<Recording, 'part' | 'offset'>[]): number | null {
  const parts = partOffsets(recordings);
  if (parts.length === 0) return chip.part === 0 ? chip.seconds : null;
  const found = parts.find((p) => p.part === chip.part);
  return found ? found.offset + chip.seconds : null;
}

/** 0: the name starts with the query, 1: one of its words does, 2: it is somewhere inside, null: no match. */
function rank(name: string, q: string): number | null {
  if (!q) return 0;
  const n = name.toLowerCase();
  if (n.startsWith(q)) return 0;
  if (n.split(/[\s._-]+/).some((w) => w.startsWith(q))) return 1;
  return n.includes(q) ? 2 : null;
}

export interface PersonChoice {
  name: string;
  hint?: string;
}

/** This meeting's people, then the others, each once (any case), filtered by `q`: better matches
 * first, this meeting's before the rest among equals; at most `limit`. */
export function pickPeople(q: string, here: readonly string[], others: readonly PersonSummary[], limit = LIMIT): PersonChoice[] {
  const want = q.trim().toLowerCase();
  const seen = new Set<string>();
  const out: { choice: PersonChoice; rank: number; order: number }[] = [];
  const add = (name: string, hint: string) => {
    const key = name.trim().toLowerCase();
    if (!key || seen.has(key)) return;
    seen.add(key);
    const r = rank(name, want);
    if (r !== null) out.push({ choice: { name: name.trim(), hint }, rank: r, order: out.length });
  };
  for (const n of here) add(n, 'In this meeting');
  for (const p of others) add(p.name, p.meetings === 1 ? '1 meeting' : `${p.meetings} meetings`);
  out.sort((a, b) => a.rank - b.rank || a.order - b.order);
  return out.slice(0, limit).map((o) => o.choice);
}

const day = (iso: string) => new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });

/** "Title · Mon D". */
export function meetingLabel(s: Pick<SessionSummary, 'name' | 'created_at'>): string {
  const when = day(s.created_at);
  return `${s.name || 'Untitled'} · ${when}`;
}

/** The meetings in the list (the ones the caller can see) whose title has `q` in it, newest first,
 * without `selfId`; at most `limit`. */
export function pickMeetings(
  q: string,
  sessions: readonly Pick<SessionSummary, 'id' | 'name' | 'created_at'>[],
  selfId: string,
  limit = LIMIT
): { id: string; label: string }[] {
  const want = q.trim().toLowerCase();
  return sessions
    .filter((s) => s.id !== selfId && (!want || (s.name || 'Untitled').toLowerCase().includes(want)))
    .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))
    .slice(0, limit)
    .map((s) => ({ id: s.id, label: meetingLabel(s) }));
}

// The People API reads every meeting: one answer is kept for a short while, since the menu asks on
// every key. On a team server the backend only counts the meetings the caller may see.
const PEOPLE_TTL_MS = 30_000;
let peopleCache: { at: number; list: PersonSummary[] } | null = null;

async function knownPeople(): Promise<PersonSummary[]> {
  if (peopleCache && Date.now() - peopleCache.at < PEOPLE_TTL_MS) return peopleCache.list;
  try {
    const list = await listPeople();
    peopleCache = { at: Date.now(), list };
    return list;
  } catch {
    return peopleCache?.list ?? [];
  }
}

/** Forget the cached people (tests). */
export function resetPeopleCache() {
  peopleCache = null;
}

export function createNotesInserts(sessionId: string): InsertProvider {
  const open = () => (sessionState.activeSession?.id === sessionId ? sessionState.activeSession : null);
  const recordingHere = () => audioState.isRecording && audioState.activeSessionId === sessionId;

  async function openMeeting(id: string) {
    if (!sessionState.sessions.some((s) => s.id === id)) await sessionState.loadSessions();
    if (!sessionState.sessions.some((s) => s.id === id)) {
      toastState.info('That meeting is not shared with you');
      return;
    }
    await sessionState.selectSession(id);
  }

  return {
    time() {
      if (recordingHere()) return audioState.clock ? recordingTime(audioState.clock, Date.now()) : null;
      const session = open();
      // The player is the open meeting's; the element exists once something was played or sought.
      if (!session || !playerState.available || !playerState.wanted) return null;
      return locate(playerState.currentTime, session.recordings);
    },

    async people(q) {
      return pickPeople(q, meetingPeople(open()), await knownPeople());
    },

    async meetings(q) {
      return pickMeetings(q, sessionState.sessions, sessionId);
    },

    onChipClick(chip: Chip) {
      if (chip.kind === 'time') {
        if (recordingHere()) {
          toastState.info('Playback after the recording stops');
          return;
        }
        const session = open();
        const target = session ? seekTarget(chip, session.recordings) : null;
        if (!session || target === null) {
          toastState.info('That moment is not in this meeting');
          return;
        }
        // As a summary item's time does (SummaryView openChapter): the transcript opens at that
        // line, flashed, and the player waits there without playing.
        // The line being said at that moment: the last one that started by then (a chip keeps
        // whole seconds, so the moment is anywhere in that second).
        const idx = session.transcript.findLastIndex((seg) => seg.start < target + 1);
        sessionState.pendingOpen = { sessionId: session.id, idx: idx < 0 ? (session.transcript.length ? 0 : null) : idx };
        if (playerState.available) playerState.seek(target, false);
      } else if (chip.kind === 'person') {
        // What the palette and the header do (controller openView('people')), without its imports.
        uiState.personRequest = chip.name;
        uiState.view = 'people';
        sessionState.show(null);
      } else {
        void openMeeting(chip.id);
      }
    }
  };
}
