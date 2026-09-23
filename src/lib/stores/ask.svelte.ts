import type { Ask, Job } from '$lib/types/index.js';
import * as api from '$lib/api/backend.js';
import { jobsState } from './jobs.svelte.js';

/** Q&A over all meetings. History is persisted by the backend; pending questions live here. */
class AskState {
  history = $state<Ask[]>([]);
  /** job id -> question, for questions still being answered */
  pending = $state<Record<string, string>>({});
  /** job id -> error, for questions that failed */
  failed = $state<Record<string, { question: string; error: string }>>({});
  draft = $state('');
  loaded = $state(false);

  private unsubscribe: (() => void) | null = null;

  init() {
    this.unsubscribe = jobsState.onComplete((job) => this.onJobComplete(job));
  }

  destroy() {
    this.unsubscribe?.();
    this.unsubscribe = null;
  }

  async load() {
    try {
      this.history = await api.listAsks();
      this.loaded = true;
    } catch {
      /* backend unreachable; keep what we have */
    }
  }

  async ask(question: string) {
    const q = question.trim();
    if (!q) return;
    const job = await api.askQuestion(q);
    jobsState.track(job);
    this.pending = { ...this.pending, [job.id]: q };
  }

  /** Called on every render to notice failures (onComplete only fires on success). */
  syncFailures() {
    for (const id of Object.keys(this.pending)) {
      const job = jobsState.jobs[id];
      if (job && (job.status === 'failed' || job.status === 'cancelled')) {
        const question = this.pending[id];
        const { [id]: _, ...rest } = this.pending;
        this.pending = rest;
        this.failed = { ...this.failed, [id]: { question, error: job.error ?? 'Cancelled' } };
      }
    }
  }

  dismissFailure(id: string) {
    const { [id]: _, ...rest } = this.failed;
    this.failed = rest;
  }

  async remove(id: string) {
    await api.deleteAsk(id);
    this.history = this.history.filter((a) => a.id !== id);
  }

  private onJobComplete(job: Job) {
    if (job.kind !== 'ask') return;
    const { [job.id]: _, ...rest } = this.pending;
    this.pending = rest;
    const result = job.result as unknown as Ask | null;
    if (result && !this.history.some((a) => a.id === result.id)) {
      this.history = [result, ...this.history];
    }
  }
}

export const askState = new AskState();
