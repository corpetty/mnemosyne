/**
 * Frontend types. REST shapes are generated from the backend's OpenAPI schema
 * (`src/lib/api/schema.d.ts`, regenerate with `scripts/gen-api-types.sh`); only the
 * WebSocket events below are written by hand.
 */
import type { components } from '$lib/api/schema.js';

type S = components['schemas'];

export type AudioDevice = S['DeviceResponse'];
export type StartRecordingResponse = S['StartRecordingResponse'];
export type StopRecordingResponse = Omit<S['StopRecordingResponse'], 'session'> & { session: SessionDetail };
export type RecordingStatus = S['RecordingStatus'];
export type ActiveRecording = S['ActiveRecording'];
export type Bookmark = S['Bookmark'];
export type SessionHistory = S['SessionHistory'];
export type HistoryPart = S['HistoryPart'];
export type HistoryEvent = S['HistoryEvent'];
export type HistoryFile = S['HistoryFile'];
export type Asset = S['Asset'];
export type ExternalNotes = S['ExternalNotes'];
export type LibraryAsset = S['LibraryAsset'];
export type AgendaItem = S['AgendaItem-Output'];
export type GlossarySuggestion = S['GlossarySuggestion'];
export type AddCorrectionResult = S['AddCorrectionResult'];
export type SessionDetail = S['Session'];
export type SessionStatus = SessionDetail['status'];
export type SessionSummary = S['SessionSummary'];
export type Me = S['Me'];
export type UserInfo = S['UserInfo'];
export type UserInvite = S['Invite'];
export type Recording = S['Recording'];
export type ActionItem = S['ActionItem'];
export type SummaryData = S['SummaryData'];
export type ClientFact = S['ClientFact'];
export type WordSegment = S['WordSegment'];
export type TranscriptSegment = S['TranscriptSegment'];
export type Job = S['Job'];
export type JobStatus = Job['status'];
export type ProviderModels = S['ProviderModels'];
export type SummaryStyle = S['SummaryStyle'];
export type SettingsValues = S['SettingsValues'];
export type SettingsResponse = S['SettingsResponse'];
/** Partial update. For secrets: '' keeps the current value, null clears it. */
export type SettingsUpdate = S['SettingsUpdate'];
export type SpeakerProfile = S['SpeakerProfileSummary'];
export type SessionSpeaker = S['SessionSpeaker'];
export type SegmentHit = S['SegmentHit'];
export type SearchHit = S['SearchHit'];
export type Citation = S['Citation'];
export type Ask = S['Ask'];
export type Digest = S['Digest'];
export type MeetingStats = S['MeetingStats'];
export type TaskItem = S['TaskItem'];
export type Brief = S['Brief'];
export type IndexStatus = S['IndexStatus'];
export type PersonSummary = S['PersonSummary'];
export type PersonDetail = S['PersonDetail'];
export type TopicCount = S['TopicCount'];
export type Thread = S['Thread'];
export type PhoneLink = S['PhoneLink'];
export type PairingCode = S['PairingCode'];
export type PairedDevice = S['PairedDeviceInfo'];
export type RemoteAccess = S['RemoteAccess'];
export type SystemInfo = S['SystemInfo'];
export type DesktopCalendar = S['DesktopCalendar'];
export type EncryptionStatus = S['EncryptionStatus'];
export type EncryptionEnabled = S['EncryptionEnabled'];
export type Quote = S['Quote'];
export type EchoCancelStatus = S['EchoCancelResponse'];
export type Diagnostics = S['Diagnostics'];
export type CopilotNotes = S['CopilotNotes'];
export type SessionUsage = S['SessionUsage'];
export type StorageReport = S['StorageReport'];
export type BackupStatus = S['BackupStatus'];
export type BackupInfo = S['BackupInfo'];
export type RestoreResponse = S['RestoreResponse'];
export type CleanupResult = S['CleanupResult'];
export type CalendarEvent = S['CalendarEvent'];
export type CalendarResponse = S['CalendarResponse'];
export type Level = S['Level'];
export type SelfTestResult = S['SelfTestResult'];
export type RepoCheck = S['RepoCheck'];
export type IssueResult = S['IssueResult'];

/** A recording interrupted by a crash, finished when the backend started again. */
export interface RecoveredRecording {
  session_id: string;
  name: string;
  seconds: number;
  transcribing: boolean;
}

/** Events pushed by the backend over /ws. */
export type BackendEvent =
  | { type: 'hello'; jobs: Job[]; recovered?: RecoveredRecording[] }
  | ({ type: 'recovered' } & RecoveredRecording)
  | { type: 'pong' }
  | { type: 'job'; job: Job }
  | { type: 'session'; session_id: string; status: SessionStatus | 'deleted' | 'audio_deleted' }
  | { type: 'status'; session_id: string | null; message: string }
  | { type: 'transcription'; session_id: string | null; segment: TranscriptSegment }
  | { type: 'error'; session_id: string | null; message: string }
  | { type: 'live_segment'; session_id: string; source: string; segment: TranscriptSegment }
  | { type: 'live_partial'; session_id: string; source: string; speaker: string; text: string }
  | { type: 'live_status'; session_id: string; message: string }
  | { type: 'live_relabel'; session_id: string; old: string; new: string }
  | {
      type: 'live_labels';
      session_id: string;
      source: string;
      labels: { start: number; old: string; new: string }[];
    }
  | { type: 'mention'; session_id: string; keyword: string; speaker: string; text: string; start: number }
  | { type: 'meeting_app'; status: 'started' | 'stopped'; app: string }
  | { type: 'copilot_notes'; session_id: string; notes: CopilotNotes }
  | { type: 'levels'; session_id: string; levels: Record<string, Level> }
  | { type: 'bookmarks'; session_id: string } // a meeting's bookmarks changed
  | { type: 'assets'; session_id: string } // a meeting's resources changed
  | { type: 'history'; session_id: string } // something was added to a meeting's history
  | {
      // A source stopped being captured, or is captured again (backend audio/health.py).
      type: 'capture_health';
      session_id: string;
      device_id: number;
      state: 'stopped' | 'stalled' | 'ok';
      message: string;
    };
