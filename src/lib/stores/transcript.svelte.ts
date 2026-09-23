import type { BackendEvent, Job, TranscriptSegment } from '$lib/types/index.js';
import * as api from '$lib/api/backend.js';
import { wsState } from './websocket.svelte.js';

const SPEAKER_COLORS = [
  'text-blue-400',
  'text-green-400',
  'text-purple-400',
  'text-orange-400',
  'text-pink-400',
  'text-cyan-400',
  'text-yellow-400',
  'text-red-400'
];

/**
 * Transcript for the session currently shown in the UI.
 *
 * Segments arrive over the WebSocket as the backend transcription job runs.
 * Only events for `sessionId` are applied, so jobs for other sessions do not
 * bleed into the view.
 */
class TranscriptState {
  segments = $state<TranscriptSegment[]>([]);
  status = $state<string>('');
  isProcessing = $state(false);
  error = $state<string | null>(null);
  sessionId = $state<string | null>(null);
  activeJob = $state<Job | null>(null);

  /** Provisional segments streamed while recording; replaced by the final job. */
  liveSegments = $state<TranscriptSegment[]>([]);
  livePartials = $state<Record<string, { speaker: string; text: string }>>({});
  liveStatus = $state<string>('');
  /** Segment index to scroll to and flash (set by search). */
  highlightIndex = $state<number | null>(null);

  private speakerColorMap = new Map<string, string>();
  private unsubscribe: (() => void) | null = null;
  private _onCompleteCallback: ((sessionId: string) => void) | null = null;

  getSpeakerColor(speaker: string): string {
    if (!this.speakerColorMap.has(speaker)) {
      const idx = this.speakerColorMap.size % SPEAKER_COLORS.length;
      this.speakerColorMap.set(speaker, SPEAKER_COLORS[idx]);
    }
    return this.speakerColorMap.get(speaker)!;
  }

  init() {
    this.unsubscribe = wsState.onMessage((raw) => this.handle(raw as BackendEvent));
  }

  destroy() {
    this.unsubscribe?.();
    this.unsubscribe = null;
  }

  onComplete(callback: (sessionId: string) => void) {
    this._onCompleteCallback = callback;
  }

  private handle(msg: BackendEvent) {
    switch (msg.type) {
      case 'hello': {
        const job = msg.jobs.find((j) => j.kind === 'transcribe' && j.session_id === this.sessionId);
        if (job) this.applyJob(job);
        break;
      }
      case 'job':
        if (msg.job.kind === 'transcribe' && msg.job.session_id === this.sessionId) {
          this.applyJob(msg.job);
        }
        break;
      case 'transcription':
        if (msg.session_id === this.sessionId) {
          this.segments = [...this.segments, msg.segment];
          this.getSpeakerColor(msg.segment.speaker);
        }
        break;
      case 'status':
        if (msg.session_id === this.sessionId) this.status = msg.message;
        break;
      case 'error':
        if (msg.session_id === this.sessionId) {
          this.error = msg.message;
          this.isProcessing = false;
        }
        break;
      case 'live_segment':
        if (msg.session_id === this.sessionId) {
          this.liveSegments = [...this.liveSegments, msg.segment].sort((a, b) => a.start - b.start);
          this.getSpeakerColor(msg.segment.speaker);
        }
        break;
      case 'live_partial':
        if (msg.session_id === this.sessionId) {
          this.livePartials = { ...this.livePartials, [msg.source]: { speaker: msg.speaker, text: msg.text } };
          this.getSpeakerColor(msg.speaker);
        }
        break;
      case 'live_status':
        if (msg.session_id === this.sessionId) this.liveStatus = msg.message;
        break;
      case 'live_relabel':
        // A live speaker was recognised or two speakers turned out to be one.
        if (msg.session_id === this.sessionId) {
          this.liveSegments = this.liveSegments.map((s) =>
            s.speaker === msg.old ? { ...s, speaker: msg.new } : s
          );
          const partials: Record<string, { speaker: string; text: string }> = {};
          for (const [k, v] of Object.entries(this.livePartials)) {
            partials[k] = v.speaker === msg.old ? { ...v, speaker: msg.new } : v;
          }
          this.livePartials = partials;
          this.getSpeakerColor(msg.new);
        }
        break;
    }
  }

  /** Recording started for `sessionId`: reset live state and follow its events. */
  startLive(sessionId: string) {
    this.sessionId = sessionId;
    this.liveSegments = [];
    this.livePartials = {};
    this.liveStatus = 'Starting...';
  }

  clearLive() {
    this.liveSegments = [];
    this.livePartials = {};
    this.liveStatus = '';
  }

  private applyJob(job: Job) {
    this.activeJob = job;
    switch (job.status) {
      case 'queued':
        this.isProcessing = true;
        this.status = 'Queued...';
        this.error = null;
        break;
      case 'running':
        this.isProcessing = true;
        if (job.message) this.status = job.message;
        break;
      case 'completed':
        this.isProcessing = false;
        this.status = 'Transcription complete';
        this._onCompleteCallback?.(job.session_id!);
        break;
      case 'failed':
        this.isProcessing = false;
        this.error = job.error ?? 'Transcription failed';
        break;
      case 'cancelled':
        this.isProcessing = false;
        this.status = 'Cancelled';
        break;
    }
  }

  /** Switch the view to a session, loading its stored transcript. */
  showSession(sessionId: string, segments: TranscriptSegment[]) {
    if (sessionId === this.sessionId && this.isProcessing) return;
    if (sessionId !== this.sessionId) this.clearLive();
    this.sessionId = sessionId;
    this.speakerColorMap.clear();
    this.segments = segments;
    for (const seg of segments) this.getSpeakerColor(seg.speaker);
    this.status = '';
    this.error = null;
    this.isProcessing = false;
    this.activeJob = null;
  }

  clear() {
    this.sessionId = null;
    this.clearLive();
    this.segments = [];
    this.speakerColorMap.clear();
    this.status = '';
    this.error = null;
    this.isProcessing = false;
    this.activeJob = null;
  }

  /** Called when a transcription job has been queued for `sessionId`. */
  expectJob(sessionId: string) {
    this.sessionId = sessionId;
    this.liveStatus = '';
    this.livePartials = {};
    this.segments = [];
    this.speakerColorMap.clear();
    this.error = null;
    this.isProcessing = true;
    this.status = 'Queued...';
  }

  /** Ask the backend to (re)transcribe the session's stored audio. */
  async transcribe(sessionId: string) {
    this.expectJob(sessionId);
    try {
      const job = await api.transcribeSession(sessionId);
      this.applyJob(job);
    } catch (e) {
      this.isProcessing = false;
      this.error = e instanceof Error ? e.message : 'Failed to start transcription';
    }
  }
}

export const transcriptState = new TranscriptState();
