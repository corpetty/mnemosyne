export interface AudioDevice {
  id: number;
  name: string;
  description: string;
  media_class: string;
  is_input: boolean;
  is_output: boolean;
  is_monitor: boolean;
}

export interface StartRecordingResponse {
  session_id: string;
  recording_id: string;
  message: string;
}

export interface StopRecordingResponse {
  session_id: string;
  recording_id: string;
  output_file: string;
  individual_files: string[];
  message: string;
}

export interface RecordingStatus {
  session_id: string;
  is_recording: boolean;
  exists: boolean;
  device_count?: number;
}

export type SessionStatus =
  | "created"
  | "recording"
  | "processing"
  | "completed"
  | "error";

export interface SessionSummary {
  id: string;
  name: string;
  status: SessionStatus;
  created_at: string;
  updated_at: string;
  has_transcript: boolean;
  has_summary: boolean;
  participant_count: number;
}

export interface SessionDetail {
  id: string;
  name: string;
  status: SessionStatus;
  created_at: string;
  updated_at: string;
  audio_file: string | null;
  transcript: TranscriptSegment[];
  summary: string;
  notes: string;
  participants: string[];
}

export interface TranscriptSegment {
  text: string;
  speaker: string;
  start: number;
  end: number;
}

export interface ProviderModels {
  provider: string;
  models: string[];
}

export interface SummarizeResponse {
  summary: string;
  provider: string;
  model: string;
}
