import type { BackendEvent, Job } from '$lib/types/index.js';
import { wsState } from './websocket.svelte.js';

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
      for (const j of msg.jobs) this.jobs[j.id] = j;
    } else if (msg.type === 'job') {
      const prev = this.jobs[msg.job.id];
      this.jobs[msg.job.id] = msg.job;
      if (msg.job.status === 'completed' && prev?.status !== 'completed') {
        for (const h of this.completeHandlers) h(msg.job);
      }
    }
  }

  private forSession(sessionId: string, kind?: string): Job[] {
    return Object.values(this.jobs)
      .filter((j) => j.session_id === sessionId && (!kind || j.kind === kind))
      .sort((a, b) => a.created_at.localeCompare(b.created_at));
  }

  /** The queued or running job for a session (optionally of one kind), if any. */
  active(sessionId: string, kind?: string): Job | null {
    return (
      this.forSession(sessionId, kind).find((j) => j.status === 'queued' || j.status === 'running') ??
      null
    );
  }

  /** The most recent job for a session of a given kind. */
  last(sessionId: string, kind: string): Job | null {
    const list = this.forSession(sessionId, kind);
    return list.length ? list[list.length - 1] : null;
  }
}

export const jobsState = new JobsState();
