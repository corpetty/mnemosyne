import { getJoined, getMoves } from '$lib/api/local.js';
import { defaultDestination, type Destination } from '$lib/app/teamDestination.js';
import { connectionState, SAME_ORIGIN } from '$lib/stores/connection.svelte.js';
import type { JoinedStatus, MovesStatus } from '$lib/types/index.js';

export const POLL_MS = 10_000;

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

  get joined(): boolean {
    return this.status?.joined === true;
  }
  /** The team's name: its hosting computer's. */
  get teamName(): string {
    return this.status?.host_name || 'the team';
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
      } catch {
        /* keep what we knew */
      } finally {
        this.loading = null;
      }
      if (this.status?.joined) this.startTimer();
      else this.stopTimer();
    })();
    return this.loading;
  }

  async loadMoves(): Promise<void> {
    try {
      this.moves = await getMoves();
    } catch {
      /* not joined, or the backend did not answer */
    }
  }

  /** A change made through the membership routes answered with the new status. */
  apply(status: JoinedStatus) {
    this.status = status;
    if (status.joined) this.startTimer();
    else {
      this.stopTimer();
      this.chosen = null;
      this.moves = null;
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
  }

  /** Forget everything (tests). */
  reset() {
    this.stopTimer();
    this.status = null;
    this.moves = null;
    this.chosen = null;
    this.loading = null;
  }
}

export const teamMemberState = new TeamMemberState();
