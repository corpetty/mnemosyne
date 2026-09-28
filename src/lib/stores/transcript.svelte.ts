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
const SPEAKER_BGS = [
  'bg-blue-400',
  'bg-green-400',
  'bg-purple-400',
  'bg-orange-400',
  'bg-pink-400',
  'bg-cyan-400',
  'bg-yellow-400',
  'bg-red-400'
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
  /** Added to live times after a restarted capture (continueLive). */
  liveOffset = 0;
  /** Start times of live lines where a mention keyword was heard. */
  mentionStarts = $state<number[]>([]);
  livePartials = $state<Record<string, { speaker: string; text: string }>>({});
  liveStatus = $state<string>('');
  /** Segment index to scroll to and flash (set by search). */
  highlightIndex = $state<number | null>(null);

  private speakerColorMap = new Map<string, string>();
  private unsubscribe: (() => void) | null = null;
  private _onCompleteCallback: ((sessionId: string) => void) | null = null;

  /** Background class matching getSpeakerColor (full names so Tailwind sees them). */
  getSpeakerBg(speaker: string): string {
    const text = this.getSpeakerColor(speaker);
    return SPEAKER_BGS[SPEAKER_COLORS.indexOf(text)] ?? 'bg-gray-500';
  }

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
        } else if (msg.job.kind === 'finish' && msg.job.session_id === this.sessionId && this.isProcessing) {
          // Encoding and mixing before the transcription job exists.
          if (msg.job.status === 'failed') {
            this.isProcessing = false;
            this.error = msg.job.error ?? 'Could not save the recording';
          } else if (msg.job.message && msg.job.status !== 'completed') {
            this.status = `${msg.job.message}…`;
          }
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
          const off = this.liveOffset;
          const seg = off ? { ...msg.segment, start: msg.segment.start + off, end: msg.segment.end + off } : msg.segment;
          this.liveSegments = [...this.liveSegments, seg].sort((a, b) => a.start - b.start);
          this.getSpeakerColor(msg.segment.speaker);
        }
        break;
      case 'live_partial':
        if (msg.session_id === this.sessionId) {
          this.livePartials = { ...this.livePartials, [msg.source]: { speaker: msg.speaker, text: msg.text } };
          this.getSpeakerColor(msg.speaker);
        }
        break;
      case 'mention':
        if (msg.session_id === this.sessionId) this.mentionStarts = [...this.mentionStarts, msg.start + this.liveOffset];
        break;
      case 'live_status':
        if (msg.session_id === this.sessionId) this.liveStatus = msg.message;
        break;
      case 'live_labels':
        // The recording so far was re-diarized: these lines have a better speaker now.
        if (msg.session_id === this.sessionId) {
          const changed = new Map(msg.labels.map((l) => [`${l.start + this.liveOffset}|${l.old}`, l.new]));
          this.liveSegments = this.liveSegments.map((s) => {
            const next = changed.get(`${s.start}|${s.speaker}`);
            return next ? { ...s, speaker: next } : s;
          });
          for (const l of msg.labels) this.getSpeakerColor(l.new);
        }
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
    this.liveOffset = 0;
    this.liveSegments = [];
    this.livePartials = {};
    this.mentionStarts = [];
    this.liveStatus = 'Starting...';
  }

  /** Pick up a live transcript already under way (see audioState.resume). */
  resumeLive(sessionId: string, segments: TranscriptSegment[], running: boolean) {
    this.startLive(sessionId);
    this.liveSegments = [...segments].sort((a, b) => a.start - b.start);
    for (const s of segments) this.getSpeakerColor(s.speaker);
    this.liveStatus = running ? 'Live' : '';
  }

  /** The capture restarted as a new part: its live times start at 0 again, `seconds` into
   *  the recording. The lines so far stay. */
  continueLive(seconds: number) {
    this.liveOffset = seconds;
    this.livePartials = {};
  }

  clearLive() {
    this.mentionStarts = [];
    this.liveSegments = [];
    this.livePartials = {};
    this.liveStatus = '';
  }

  private applyJob(job: Job) {
    const previous = this.activeJob;
    this.activeJob = job;
    if (previous?.id === job.id && previous.status === job.status && job.status !== 'running') return;
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
  expectJob(sessionId: string, status = 'Queued...') {
    this.sessionId = sessionId;
    this.liveStatus = '';
    this.livePartials = {};
    this.segments = [];
    this.speakerColorMap.clear();
    this.error = null;
    this.isProcessing = true;
    this.status = status;
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
