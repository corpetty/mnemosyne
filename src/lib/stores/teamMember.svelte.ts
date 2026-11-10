import { getJoined, getMoves } from '$lib/api/local.js';
import { defaultDestination, teamLabel, type Destination } from '$lib/app/teamDestination.js';
import { connectionState, SAME_ORIGIN } from '$lib/stores/connection.svelte.js';
import type { JoinedStatus, MovedMeeting, MovesStatus } from '$lib/types/index.js';

export const POLL_MS = 10_000;
/** After a meeting was put in the team: look this often, for this long, for it to arrive. */
export const FAST_MS = 2000;
export const FAST_FOR_MS = 120_000;

/**
 * This computer in a team another computer hosts (backend services/team_member.py, `/api/joined`):
 * who it is there, whether the host can be reached, where team recordings are done by default, and
 * the meetings waiting to move to the team. Always asked of this computer's own backend, whichever
 * library the window shows: the state of the membership is what lets the window show the switch at
 * all, and say so when the team cannot be reached.
 */
class TeamMemberState {
  status = $state<JoinedStatus | null>(null);
  moves = $state<MovesStatus | null>(null);
  /** The Record menu's choice for the next recording; null = the default. It lasts for that one. */
  chosen = $state<Destination | null>(null);
  private timer: ReturnType<typeof setInterval> | null = null;
  private loading: Promise<void> | null = null;
  private tries = 0;
  /** Meetings known to have moved (null until the first look): only a move seen after it is news. */
  private movedSeen: Set<string> | null = null;
  private pendingBefore = 0;
  private fast: ReturnType<typeof setInterval> | null = null;
  /** A meeting recorded or put here has arrived in the team (it is gone from this library). */
  onMoved: ((m: MovedMeeting) => void) | null = null;
  private retry: ReturnType<typeof setTimeout> | null = null;

  get joined(): boolean {
    return this.status?.joined === true;
  }
  /** The team's name, "Team on <host>". */
  get teamName(): string {
    return teamLabel(this.status?.host_name);
  }
  /** The hosting computer's own name. */
  get hostName(): string {
    return this.status?.host_name || 'the host';
  }
  get reachable(): boolean {
    return this.status?.reachable === true;
  }
  get signedOut(): boolean {
    return this.status?.signed_out === true;
  }
  get pending(): number {
    return this.status?.pending_moves ?? 0;
  }
  get work(): 'here' | 'host' {
    return this.status?.work ?? 'here';
  }
  /** The host's rule: each recording asks how its people agreed to it. */
  get hostConsentRequired(): boolean {
    return this.status?.consent_required === true;
  }
  /** Where the next recording goes: the menu's choice, else the default for the library shown. */
  get destination(): Destination {
    if (!this.joined) return 'mine';
    return this.chosen ?? defaultDestination(connectionState.library, this.work);
  }

  choose(d: Destination | null) {
    this.chosen = d;
  }

  /** Ask now; shares a request already on its way. A backend that does not answer leaves the last
   * state (the window has bigger problems than the team). */
  load(): Promise<void> {
    if (SAME_ORIGIN) return Promise.resolve(); // a team server's page has no computer of its own to ask
    if (this.loading) return this.loading;
    this.loading = (async () => {
      try {
        this.status = await getJoined();
        this.tries = 0;
      } catch {
        // Keep what we knew. A window that has not heard yet (the backend is still starting) asks again.
        if (this.status === null && this.tries++ < 40 && !this.retry) {
          this.retry = setTimeout(() => {
            this.retry = null;
            void this.load();
          }, 3000);
        }
      } finally {
        this.loading = null;
      }
      if (this.status?.joined) {
        this.startTimer();
        // Moves finish quietly: look at the list while meetings wait, just after the last one went,
        // and once to know what had moved before.
        const waiting = this.status.pending_moves;
        if (this.movedSeen === null || waiting > 0 || this.pendingBefore > 0) await this.loadMoves();
        this.pendingBefore = waiting;
        if (waiting === 0) this.stopFast();
      } else this.stopTimer();
    })();
    return this.loading;
  }

  async loadMoves(): Promise<void> {
    try {
      const moves = await getMoves();
      this.moves = moves;
      const seen = this.movedSeen;
      this.movedSeen = new Set(moves.moved.map((m) => m.session_id));
      if (seen === null) return;
      for (const m of [...moves.moved].reverse()) if (!seen.has(m.session_id)) this.onMoved?.(m);
    } catch {
      /* not joined, or the backend did not answer */
    }
  }

  /** A meeting was just put in the team: look closely for a while, so the window hears of its arrival. */
  expectMove() {
    if (this.fast) return;
    const until = Date.now() + FAST_FOR_MS;
    this.fast = setInterval(() => {
      if (Date.now() > until) this.stopFast();
      else void this.load();
    }, FAST_MS);
  }
  private stopFast() {
    if (this.fast) clearInterval(this.fast);
    this.fast = null;
  }

  /** A change made through the membership routes answered with the new status. */
  apply(status: JoinedStatus) {
    this.status = status;
    if (status.joined) this.startTimer();
    else {
      this.stopTimer();
      this.chosen = null;
      this.moves = null;
      this.movedSeen = null;
      this.pendingBefore = 0;
      this.stopFast();
    }
  }

  private startTimer() {
    if (this.timer) return;
    this.timer = setInterval(() => void this.load(), POLL_MS);
  }
  private stopTimer() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  stop() {
    this.stopTimer();
    this.stopFast();
    if (this.retry) clearTimeout(this.retry);
    this.retry = null;
  }

  /** Forget everything (tests). */
  reset() {
    this.stop();
    this.tries = 0;
    this.status = null;
    this.moves = null;
    this.chosen = null;
    this.loading = null;
    this.movedSeen = null;
    this.pendingBefore = 0;
    this.onMoved = null;
  }
}

export const teamMemberState = new TeamMemberState();
