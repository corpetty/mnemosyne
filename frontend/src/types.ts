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
}

export enum UploadMode {
  Recording,
  FileUpload,
  Resummarize,
}
