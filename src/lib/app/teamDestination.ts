/**
 * Where a recording goes when this computer has joined a team (docs/plans/2026-11-10-team-members-in-
 * the-app.md 1.3): the member's own library, the team with the work done on this computer (recorded
 * here, moved to the team once transcribed), or the team with the work done on the host's computer
 * (streamed as it is recorded). Small pure functions, so the rules can be tested.
 */
import type { Library } from '$lib/stores/connection.svelte.js';

export type Destination = 'mine' | 'team-here' | 'team-host';
/** The member's default for a team recording (`/api/joined` `work`). */
export type Work = 'here' | 'host';

/** What the team is called wherever it is named: "Team on <host>", its hosting computer's name (the
 * one helper every label goes through). Before the host's name is known: "the team". */
export function teamLabel(host: string | null | undefined): string {
  const h = host?.trim();
  return h ? `Team on ${h}` : 'the team';
}

/** What the Record button does when its menu was not touched: your own library records into your
 * own library; the team's library records for the team the way the member chose in Settings. */
export function defaultDestination(library: Library, work: Work): Destination {
  if (library === 'mine') return 'mine';
  return work === 'host' ? 'team-host' : 'team-here';
}

/** The library the window must show for the recording to be followed in it. A recording made on
 * this computer (own library, or for the team) is on this computer's own backend; one streamed to
 * the host is a meeting of the team's library (live transcript, notes). */
export function libraryFor(destination: Destination): Library {
  return destination === 'team-host' ? 'team' : 'mine';
}

/** Does this recording ask how its people agreed to it? A team recording follows the HOST's rule,
 * wherever it is made; the member's own library keeps the rule of the backend the window is on. */
export function consentRequiredFor(destination: Destination, connected: boolean, host: boolean): boolean {
  return destination === 'mine' ? connected : host;
}

// ---- the draft a recording leaves behind ---------------------------------------------------------

/** What of a meeting says it was touched (backend models/session.py): a new meeting has the default
 * name and none of these. */
export interface DraftFacts {
  name: string;
  status?: string;
  audio_file?: string | null;
  recordings?: unknown[];
  transcript?: unknown[];
  summary?: string;
  notes?: string;
  own_notes?: string | null;
  participants?: unknown[];
  attendees?: unknown[];
  agenda?: unknown[];
  assets?: unknown[];
  external_notes?: unknown[];
  bookmarks?: unknown[];
  copilot_notes?: unknown;
  meeting_type?: string;
  shared_from?: string;
  origin?: string;
}

export const DEFAULT_SESSION_NAME = 'Untitled Session';

/** A meeting nobody has touched: "New meeting", still with its default name and nothing in it (no
 * title change, agenda, resources, notes, other assistants' notes, audio or transcript). */
export function isEmptyDraft(s: DraftFacts | null | undefined): boolean {
  if (!s) return false;
  const none = (a: unknown[] | undefined) => !a || a.length === 0;
  return (
    s.name === DEFAULT_SESSION_NAME &&
    (!s.status || s.status === 'created') &&
    !s.audio_file &&
    none(s.recordings) &&
    none(s.transcript) &&
    !s.summary &&
    !s.notes?.trim() &&
    !s.own_notes?.trim() &&
    none(s.participants) &&
    none(s.attendees) &&
    none(s.agenda) &&
    none(s.assets) &&
    none(s.external_notes) &&
    none(s.bookmarks) &&
    !s.copilot_notes &&
    !s.meeting_type &&
    !s.shared_from &&
    !s.origin
  );
}

/** What a recording does with the meeting that is open in the library shown:
 * - use: it is an empty draft and the recording goes into this library, so it is recorded into;
 * - remove: it is an empty draft but the recording goes into the other library (the window switches),
 *   so it would stay behind empty: it is removed;
 * - keep: it has content, it is not touched. */
export type DraftPlan = 'use' | 'remove' | 'keep';

export function draftPlan(shown: Library, destination: Destination, open: DraftFacts | null | undefined): DraftPlan {
  if (!isEmptyDraft(open)) return 'keep';
  return libraryFor(destination) === shown ? 'use' : 'remove';
}

/** The team destinations. */
export const isTeamDestination = (d: Destination): boolean => d !== 'mine';

/** A move the team refused for want of consent (backend api/routes/handoff.py): the person can add
 * how they agreed. */
export function moveNeedsConsent(lastError: string): boolean {
  return /agreed to be recorded/i.test(lastError);
}

/** Why a meeting of the library cannot be put in the team ('' = it can), from what the list knows.
 * The backend's refusal (services/team_move.py) is the last word. */
export function putInTeamReason(
  session: { origin?: string; shared_from?: string } | null | undefined
): string {
  if (!session) return 'No meeting is open';
  if (session.shared_from) return 'This meeting was shared with you: it is not yours to put in the team';
  if (session.origin) return 'This meeting was recorded on another computer: its audio is not here';
  return '';
}

// ---- a recording started from the wrong library --------------------------------------------------
// The window follows a recording in one library, and switching reloads it: so a Record that needs the
// other library switches first and leaves this note for the next page to start the recording.

const PENDING_KEY = 'mnemosyne.pendingRecord';
const PENDING_MS = 60_000;

export interface PendingStart {
  destination: Destination;
  fresh: boolean;
}

export function savePendingStart(p: PendingStart, store: Storage | undefined = safeSession(), now = Date.now()) {
  try {
    store?.setItem(PENDING_KEY, JSON.stringify({ ...p, at: now }));
  } catch {
    /* no storage: the person presses Record again */
  }
}

/** The note left before a library switch, once (a note older than a minute is stale). */
export function takePendingStart(store: Storage | undefined = safeSession(), now = Date.now()): PendingStart | null {
  try {
    const raw = store?.getItem(PENDING_KEY);
    store?.removeItem(PENDING_KEY);
    if (!raw) return null;
    const p = JSON.parse(raw);
    const ok = ['mine', 'team-here', 'team-host'].includes(p?.destination);
    if (!ok || typeof p.at !== 'number' || now - p.at > PENDING_MS) return null;
    return { destination: p.destination, fresh: p.fresh === true };
  } catch {
    return null;
  }
}

function safeSession(): Storage | undefined {
  try {
    return typeof sessionStorage === 'undefined' ? undefined : sessionStorage;
  } catch {
    return undefined;
  }
}

// ---- opening a meeting after the window switched library -----------------------------------------

const OPEN_KEY = 'mnemosyne.openAfterSwitch';

/** The id (in the library about to be shown) of a meeting to open once the switch has reloaded. */
export function saveOpenAfterSwitch(id: string, store: Storage | undefined = safeSession(), now = Date.now()) {
  try {
    store?.setItem(OPEN_KEY, JSON.stringify({ id, at: now }));
  } catch {
    /* no storage: the person finds it in the list */
  }
}

export function takeOpenAfterSwitch(store: Storage | undefined = safeSession(), now = Date.now()): string | null {
  try {
    const raw = store?.getItem(OPEN_KEY);
    store?.removeItem(OPEN_KEY);
    if (!raw) return null;
    const p = JSON.parse(raw);
    return typeof p?.id === 'string' && p.id && typeof p.at === 'number' && now - p.at <= PENDING_MS ? p.id : null;
  } catch {
    return null;
  }
}
