import type { BackendEvent, Job } from '$lib/types/index.js';
import { getJob } from '$lib/api/backend.js';
import { wsState } from './websocket.svelte.js';

const KEEP_FINISHED = 200;
const running = (j: Job) => j.status === 'queued' || j.status === 'running';

type CompleteHandler = (job: Job) => void;

/** Every job the backend has told us about, kept current from WebSocket events. */
class JobsState {
  jobs = $state<Record<string, Job>>({});

  private unsubscribe: (() => void) | null = null;
  private completeHandlers: CompleteHandler[] = [];

  init() {
    this.unsubscribe = wsState.onMessage((raw) => this.handle(raw as BackendEvent));
  }

  destroy() {
    this.unsubscribe?.();
    this.unsubscribe = null;
    this.completeHandlers = [];
  }

  /** Called once per job when it reaches `completed`. Returns an unsubscribe function. */
  onComplete(cb: CompleteHandler): () => void {
    this.completeHandlers.push(cb);
    return () => {
      this.completeHandlers = this.completeHandlers.filter((h) => h !== cb);
    };
  }

  /**
   * Record a job we just created over HTTP. Fast jobs can finish before the HTTP
   * response arrives; a state already received over the WebSocket is newer, so keep it.
   */
  track(job: Job) {
    if (!this.jobs[job.id]) this.jobs[job.id] = job;
  }

  isDone(id: string): boolean {
    return this.jobs[id]?.status === 'completed';
  }

  private handle(msg: BackendEvent) {
    if (msg.type === 'hello') {
      // (Re)connected. hello lists the jobs still running; one we thought was running and
      // is not listed ended while we were away, or with a backend that restarted.
      const listed = new Set(msg.jobs.map((j) => j.id));
      const missed = Object.values(this.jobs).filter((j) => running(j) && !listed.has(j.id));
      for (const j of msg.jobs) this.jobs[j.id] = j;
      for (const j of missed) void this.catchUp(j);
    } else if (msg.type === 'job') {
      this.apply(msg.job);
    }
  }

  private apply(job: Job) {
    const prev = this.jobs[job.id];
    if (!prev) this.prune();
    this.jobs[job.id] = job;
    if (job.status === 'completed' && prev?.status !== 'completed') {
      for (const h of this.completeHandlers) h(job);
    }
  }

  private async catchUp(job: Job) {
    try {
      this.apply(await getJob(job.id));
    } catch {
      // Unknown to the backend: it restarted, and the job went with it.
      this.jobs[job.id] = { ...job, status: 'failed', error: 'Interrupted: the backend restarted' };
    }
  }

  /** Forget the oldest finished jobs: a window left open for weeks sees thousands. */
  private prune() {
    const finished = Object.values(this.jobs).filter((j) => !running(j));
    if (finished.length <= KEEP_FINISHED) return;
    finished.sort((a, b) => a.created_at.localeCompare(b.created_at));
    for (const j of finished.slice(0, finished.length - KEEP_FINISHED)) delete this.jobs[j.id];
  }

  private forSession(sessionId: string, kind?: string): Job[] {
    return Object.values(this.jobs)
      .filter((j) => j.session_id === sessionId && (!kind || j.kind === kind))
      .sort((a, b) => a.created_at.localeCompare(b.created_at));
  }

  /** The queued or running job for a session (optionally of one kind), if any. */
  active(sessionId: string, kind?: string): Job | null {
    return this.forSession(sessionId, kind).find(running) ?? null;
  }

  /** The most recent job for a session of a given kind. */
  last(sessionId: string, kind: string): Job | null {
    const list = this.forSession(sessionId, kind);
    return list.length ? list[list.length - 1] : null;
  }
}

export const jobsState = new JobsState();
