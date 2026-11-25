import { getJob } from '$lib/api/backend.js';
import { jobsState } from '$lib/stores/jobs.svelte.js';
import type { Job } from '$lib/types/index.js';

/** Wait for a job to end, from the WebSocket's view of it when that is newer, else by asking. */
export async function waitForJob(job: Job, everyMs = 700): Promise<Job> {
  jobsState.track(job);
  for (;;) {
    const seen = jobsState.jobs[job.id];
    const current = seen && seen.status !== 'queued' && seen.status !== 'running' ? seen : await getJob(job.id).catch(() => seen ?? job);
    if (current.status !== 'queued' && current.status !== 'running') return current;
    await new Promise((r) => setTimeout(r, everyMs));
  }
}

/**
 * The microphone, recorded as it is: no echo cancellation, noise suppression or gain control,
 * which change a voice (a narration voice is learnt from this). Nothing is played.
 */
export class VoiceRecorder {
  private recorder: MediaRecorder | null = null;
  private stream: MediaStream | null = null;
  private parts: Blob[] = [];
  startedAt = 0;

  get recording(): boolean {
    return this.recorder?.state === 'recording';
  }

  async start(): Promise<void> {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1 }
    });
    this.parts = [];
    this.recorder = new MediaRecorder(this.stream);
    this.recorder.ondataavailable = (e) => {
      if (e.data.size) this.parts.push(e.data);
    };
    this.recorder.start();
    this.startedAt = performance.now();
  }

  /** The take, and a file name that tells the backend its format. */
  stop(): Promise<{ blob: Blob; filename: string; seconds: number }> {
    const recorder = this.recorder;
    if (!recorder) return Promise.reject(new Error('Not recording'));
    const seconds = (performance.now() - this.startedAt) / 1000;
    return new Promise((resolve) => {
      recorder.onstop = () => {
        this.stream?.getTracks().forEach((t) => t.stop());
        this.stream = null;
        this.recorder = null;
        const type = recorder.mimeType || 'audio/webm';
        const ext = type.includes('ogg') ? 'ogg' : type.includes('mp4') ? 'm4a' : 'webm';
        resolve({ blob: new Blob(this.parts, { type }), filename: `take.${ext}`, seconds });
      };
      recorder.stop();
    });
  }

  cancel() {
    if (this.recorder && this.recorder.state !== 'inactive') this.recorder.stop();
    this.stream?.getTracks().forEach((t) => t.stop());
    this.recorder = null;
    this.stream = null;
  }
}

/** "400: The voice is not ready" -> "The voice is not ready". */
export function plainError(e: unknown, fallback: string): string {
  const text = e instanceof Error ? e.message : String(e ?? '');
  return text.replace(/^\d{3}: /, '') || fallback;
}
