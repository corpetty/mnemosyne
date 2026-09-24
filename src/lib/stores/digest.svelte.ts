import type { Digest, Job } from '$lib/types/index.js';
import * as api from '$lib/api/backend.js';
import { jobsState } from './jobs.svelte.js';

/** Digests of a week (or any range) of meetings. History is persisted by the backend. */
class DigestState {
  history = $state<Digest[]>([]);
  loaded = $state(false);
  /** job id of the digest being written, if any */
  pendingJob = $state<string | null>(null);
  error = $state('');
  selectedId = $state<string | null>(null);

  private unsubscribe: (() => void) | null = null;

  init() {
    this.unsubscribe = jobsState.onComplete((job) => this.onJobComplete(job));
  }

  destroy() {
    this.unsubscribe?.();
    this.unsubscribe = null;
  }

  get selected(): Digest | null {
    return this.history.find((d) => d.id === this.selectedId) ?? this.history[0] ?? null;
  }

  async load() {
    try {
      const fetched = await api.listDigests();
      // A digest can land while this request is in flight; merge rather than replace.
      const seen = new Set(fetched.map((d) => d.id));
      this.history = [...this.history.filter((d) => !seen.has(d.id)), ...fetched].sort((a, b) =>
        a.start < b.start ? 1 : a.start > b.start ? -1 : 0
      );
      this.loaded = true;
    } catch {
      /* backend unreachable; keep what we have */
    }
  }

  async generate(start: string, end: string) {
    this.error = '';
    const job = await api.createDigest(start, end);
    jobsState.track(job);
    if (!jobsState.isDone(job.id)) this.pendingJob = job.id;
  }

  /** Called on render to notice failures (onComplete only fires on success). */
  syncFailure() {
    const job = this.pendingJob ? jobsState.jobs[this.pendingJob] : null;
    if (job && (job.status === 'failed' || job.status === 'cancelled')) {
      this.error = job.error ?? 'Cancelled';
      this.pendingJob = null;
    }
  }

  async remove(id: string) {
    await api.deleteDigest(id);
    this.history = this.history.filter((d) => d.id !== id);
    if (this.selectedId === id) this.selectedId = null;
  }

  private onJobComplete(job: Job) {
    if (job.kind !== 'digest') return;
    if (job.id === this.pendingJob) this.pendingJob = null;
    const result = job.result as unknown as Digest | null;
    if (!result) return;
    // A scheduled digest arrives here too.
    this.history = [result, ...this.history.filter((d) => d.id !== result.id && d.label !== result.label)].sort((a, b) =>
      a.start < b.start ? 1 : a.start > b.start ? -1 : 0
    );
    this.selectedId = result.id;
  }
}

export const digestState = new DigestState();
