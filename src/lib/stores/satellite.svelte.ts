import { getSatellite } from '$lib/api/local.js';
import { connectionState, LOCAL_BACKEND, SAME_ORIGIN } from '$lib/stores/connection.svelte.js';
import type { OutboxEntry, SatelliteRecording, SatelliteStatus } from '$lib/types/index.js';

export const POLL_MS = 2000;

/** The window's connection is this computer's own backend: the plain address, or the team
 * library's (`/api/joined` on it: the backend proxies the team there). Either way the local
 * backend is the one to ask about the outbox. */
export function onLocalBackend(url: string): boolean {
  const u = url.replace(/\/+$/, '');
  return u === LOCAL_BACKEND || u === `${LOCAL_BACKEND}/api/joined`;
}
/** Failed polls in a row before the local backend counts as not answering (one blip is not). */
const FAILS = 2;

/**
 * A computer that uses a main computer records through its OWN backend (backend services/
 * satellite.py): what it is recording, whether the main computer can be reached from it, and the
 * recordings waiting in its outbox. The window's main connection may be down or pointing at the
 * main computer, so this asks the local backend directly (api/local.ts). Polled every 2 s in the
 * uses-main role, and in any role while a recording or an outbox item exists; else stopped.
 */
class SatelliteState {
  status = $state<SatelliteStatus | null>(null);
  /** The last poll's failure ('' = fine). */
  error = $state('');
  private usesMain = false;
  private timer: ReturnType<typeof setInterval> | null = null;
  private loading: Promise<void> | null = null;
  private fails = 0;

  /** This computer's own backend answered. */
  get localUp(): boolean {
    return this.status !== null;
  }
  get mainName(): string {
    return this.status?.main?.name || 'the main computer';
  }
  /** The team this computer is in (None: not in one, or signed out). */
  get teamName(): string {
    return this.status?.team?.name || 'the team';
  }
  get inTeam(): boolean {
    return this.status?.team != null;
  }
  /** What to call where a recording goes: the main computer's name, or the team's. */
  nameFor(destination: 'main' | 'team'): string {
    return destination === 'team' ? this.teamName : this.mainName;
  }
  /** This computer is in a team and its host cannot be reached from here right now. */
  get teamDown(): boolean {
    return this.status !== null && this.status.team !== null && !this.status.team_reachable;
  }
  /** Recordings waiting for (or being handed to) `destination`. */
  waitingFor(destination: 'main' | 'team'): OutboxEntry[] {
    return this.waiting.filter((e) => e.destination === destination);
  }
  get recording(): SatelliteRecording | null {
    return this.status?.recording ?? null;
  }
  /** Recordings waiting for (or being handed to) the main computer; the one being made is not. */
  get waiting(): OutboxEntry[] {
    return (this.status?.outbox ?? []).filter((e) => e.state !== 'recording');
  }
  /** This computer uses a main computer and cannot reach it right now (the local backend says so,
   * which also holds while the window's own connection to it hangs rather than fails). */
  get mainDown(): boolean {
    return this.usesMain && this.status !== null && this.status.main !== null && !this.status.reachable;
  }
  private get busy(): boolean {
    return !!this.status && (!!this.status.recording || this.status.outbox.length > 0);
  }

  /** The window's role changed (or is known): poll while it uses a main computer. */
  follow(usesMain: boolean) {
    this.usesMain = usesMain;
    if (usesMain) {
      this.startTimer();
      void this.refresh();
      return;
    }
    // On its own (or a main computer): one look for audio still waiting from before, and carry
    // on only while there is some. A window on another backend has no local outbox to ask about.
    if (SAME_ORIGIN || !onLocalBackend(connectionState.url)) {
      this.stop();
      return;
    }
    void this.refresh();
  }

  stop() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  private startTimer() {
    if (this.timer) return;
    this.timer = setInterval(() => void this.refresh(), POLL_MS);
  }

  /** Ask now (after Start, Stop, an outbox change); shares a request already on its way. */
  refresh(): Promise<void> {
    if (this.loading) return this.loading;
    this.loading = (async () => {
      try {
        this.status = await getSatellite();
        this.error = '';
        this.fails = 0;
      } catch (e) {
        this.error = e instanceof Error ? e.message : 'The backend on this computer did not answer';
        if (++this.fails >= FAILS) this.status = null;
      } finally {
        this.loading = null;
      }
      if (!this.usesMain) {
        if (this.busy) this.startTimer();
        else this.stop();
      }
    })();
    return this.loading;
  }

  /** Forget everything (tests, a reload of the role). */
  reset() {
    this.stop();
    this.status = null;
    this.error = '';
    this.fails = 0;
    this.usesMain = false;
    this.loading = null;
  }
}

export const satelliteState = new SatelliteState();
