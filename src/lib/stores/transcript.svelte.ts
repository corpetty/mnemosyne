import { wsState } from "./websocket.svelte.js";

export interface TranscriptSegment {
  text: string;
  speaker: string;
  start: number;
  end: number;
}

const SPEAKER_COLORS = [
  "text-blue-400",
  "text-green-400",
  "text-purple-400",
  "text-orange-400",
  "text-pink-400",
  "text-cyan-400",
  "text-yellow-400",
  "text-red-400",
];

class TranscriptState {
  segments = $state<TranscriptSegment[]>([]);
  status = $state<string>("");
  isProcessing = $state(false);
  error = $state<string | null>(null);

  private speakerColorMap = new Map<string, string>();
  private unsubscribe: (() => void) | null = null;
  private _onCompleteCallback: (() => void) | null = null;

  get speakerColors(): Map<string, string> {
    return this.speakerColorMap;
  }

  getSpeakerColor(speaker: string): string {
    if (!this.speakerColorMap.has(speaker)) {
      const idx = this.speakerColorMap.size % SPEAKER_COLORS.length;
      this.speakerColorMap.set(speaker, SPEAKER_COLORS[idx]);
    }
    return this.speakerColorMap.get(speaker)!;
  }

  init() {
    this.unsubscribe = wsState.onMessage((msg) => {
      const type = msg.type as string;

      if (type === "transcription") {
        const seg = msg.segment as TranscriptSegment;
        this.segments = [...this.segments, seg];
        this.getSpeakerColor(seg.speaker);
      } else if (type === "status") {
        this.status = msg.message as string;
        if (this.status === "Transcribing...") {
          this.isProcessing = true;
        } else if (this.status === "Transcription complete") {
          this.isProcessing = false;
          // Notify listeners that transcription is done
          this._onCompleteCallback?.();
        }
      } else if (type === "error") {
        this.error = msg.message as string;
        this.isProcessing = false;
      }
    });
  }

  destroy() {
    this.unsubscribe?.();
    this.unsubscribe = null;
  }

  onComplete(callback: () => void) {
    this._onCompleteCallback = callback;
  }

  clear() {
    this.segments = [];
    this.speakerColorMap.clear();
    this.status = "";
    this.error = null;
    this.isProcessing = false;
  }

  loadFromSession(segments: TranscriptSegment[]) {
    this.speakerColorMap.clear();
    for (const seg of segments) {
      this.getSpeakerColor(seg.speaker);
    }
    this.segments = segments;
  }

  startTranscription(audioPath: string, sessionId?: string) {
    this.clear();
    this.isProcessing = true;
    wsState.send({
      type: "transcribe",
      audio_path: audioPath,
      session_id: sessionId,
    });
  }
}

export const transcriptState = new TranscriptState();
