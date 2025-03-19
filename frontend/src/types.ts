export interface TranscriptSegment {
  text: string;
  timestamp: number;
  speaker: string;
  start?: number;
  end?: number;
}

export interface AudioDevice {
  id: string | number;
  name: string;
  channels: number;
  default: boolean;
  is_monitor?: boolean;
}

export interface Model {
  id: string;
  name: string;
  provider: string;
  size?: string;
}

export interface TranscriptFile {
  path: string;
  filename: string;
  date: string;
  size: number;
  session_id?: string;
}

export enum UploadMode {
  Recording,
  FileUpload,
  Resummarize,
}

export enum SessionStatus {
  Created = "created",
  Recording = "recording",
  Processing = "processing",
  Completed = "completed",
  Error = "error"
}

export interface Participant {
  id: string;
  name: string;
}

export interface Session {
  session_id: string;
  created_at: string;
  status: string;
  model?: string;
  device_ids: string[];
  recording_file?: string;
  transcript_file?: string;
  is_recording: boolean;
  summary_length?: number;
  transcript_length?: number;
  name?: string;
  participants?: Participant[];
}
