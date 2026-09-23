export interface AudioDevice {
  id: number;
  name: string;
  description: string;
  media_class: string;
  is_input: boolean;
  is_output: boolean;
  is_monitor: boolean;
  is_echo_cancelled: boolean;
}

export interface StartRecordingResponse {
  session_id: string;
  recording_id: string;
  live_job_id: string | null;
  message: string;
}

export interface StopRecordingResponse {
  session: SessionDetail;
  job_id: string | null;
  message: string;
}

export interface RecordingStatus {
  session_id: string;
  is_recording: boolean;
  exists: boolean;
  device_count?: number;
}

export type SessionStatus =
  | 'created'
  | 'recording'
  | 'encoding'
  | 'transcribing'
  | 'completed'
  | 'error';

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

export interface Recording {
  id: string;
  source: 'mic' | 'system' | 'import';
  device_id: number;
  device_name: string;
  path: string;
  created_at: string;
}

export interface ActionItem {
  text: string;
  owner: string | null;
}

export interface SummaryData {
  style: string;
  provider: string;
  model: string;
  topics: string[];
  decisions: string[];
  action_items: ActionItem[];
  open_questions: string[];
}

export interface SessionDetail {
  id: string;
  name: string;
  status: SessionStatus;
  created_at: string;
  updated_at: string;
  audio_file: string | null;
  recordings: Recording[];
  transcript: TranscriptSegment[];
  summary: string;
  summary_data: SummaryData | null;
  notes: string;
  participants: string[];
}

export interface WordSegment {
  word: string;
  start: number;
  end: number;
  score: number;
}

export interface TranscriptSegment {
  text: string;
  speaker: string;
  start: number;
  end: number;
  words?: WordSegment[] | null;
}

export type JobStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';

export interface Job {
  id: string;
  kind: string;
  session_id: string | null;
  status: JobStatus;
  message: string;
  progress: number | null;
  error: string | null;
  result: Record<string, unknown> | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface ProviderModels {
  provider: string;
  models: string[];
}

export interface SummaryStyle {
  id: string;
  description: string;
}

export interface SettingsValues {
  data_dir: string;
  transcriber: 'whisperx' | 'parakeet' | 'remote';
  diarizer: 'pyannote' | 'none';
  language: string;
  min_speakers: number | null;
  max_speakers: number | null;
  auto_transcribe: boolean;
  auto_summarize: boolean;
  local_speaker_name: string;
  remote_speaker_name: string;
  echo_cancel: boolean;
  echo_dedup: boolean;
  api_token: string;
  echo_similarity: number;
  auto_label_speakers: boolean;
  speaker_match_threshold: number;
  per_source_transcription: boolean;
  live_transcription: boolean;
  live_transcriber: 'parakeet' | 'whisperx' | 'remote';
  live_interval_seconds: number;
  hf_token: string;
  whisper_model_size: string;
  whisper_compute_type: string;
  whisper_batch_size: number;
  parakeet_model: string;
  parakeet_quantization: string;
  onnx_provider: 'cpu' | 'cuda';
  remote_stt_url: string;
  remote_stt_model: string;
  remote_stt_api_key: string;
  diarization_model: string;
  ollama_url: string;
  vllm_url: string;
  openai_api_key: string;
  anthropic_api_key: string;
  default_provider: string;
  default_model: string;
  summary_style: string;
  summary_instructions: string;
  obsidian_vault_path: string;
  obsidian_subfolder: string;
  obsidian_tags: string;
  obsidian_link_people: boolean;
  obsidian_include_transcript: boolean;
}

export interface SettingsResponse {
  values: SettingsValues;
  secrets_set: Record<string, boolean>;
  env_overrides: string[];
  config_file: string;
  obsidian_vault_exists: boolean;
}

/** Partial update. For secrets: '' keeps the current value, null clears it. */
export type SettingsUpdate = Partial<{
  [K in keyof Omit<SettingsValues, 'data_dir'>]: SettingsValues[K] | null;
}>;

/** Events pushed by the backend over /ws. */
export type BackendEvent =
  | { type: 'hello'; jobs: Job[] }
  | { type: 'pong' }
  | { type: 'job'; job: Job }
  | { type: 'session'; session_id: string; status: SessionStatus | 'deleted' }
  | { type: 'status'; session_id: string | null; message: string }
  | { type: 'transcription'; session_id: string | null; segment: TranscriptSegment }
  | { type: 'error'; session_id: string | null; message: string }
  | { type: 'live_segment'; session_id: string; source: string; segment: TranscriptSegment }
  | { type: 'live_partial'; session_id: string; source: string; speaker: string; text: string }
  | { type: 'live_status'; session_id: string; message: string };

export interface SpeakerProfile {
  id: string;
  name: string;
  sample_count: number;
  created_at: string;
  updated_at: string;
}

export interface SessionSpeaker {
  label: string;
  has_voice: boolean;
}

export interface SegmentHit {
  idx: number;
  speaker: string;
  start: number;
  snippet: string; // matches wrapped in [[ ]]
}

export interface SearchHit {
  session_id: string;
  session_name: string;
  created_at: string;
  score: number;
  session_snippet: string | null;
  segments: SegmentHit[];
}
