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

export enum UploadMode {
  Recording,
  FileUpload,
}
