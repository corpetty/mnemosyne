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
export type LiveFlag = S['LiveFlag'];
export type Comment = S['Comment'];
export type CommentThread = S['CommentThread'];
export type SessionComments = S['SessionComments'];
export type ObjectComments = S['ObjectComments'];
export type CommentCreate = S['CommentCreate'];
export type CommentUpdate = S['CommentUpdate'];
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
export type MeetingRecord = S['MeetingRecord'];
export type RecordVersion = S['Version'];
export type VersionInfo = S['VersionInfo'];
export type Deletion = S['Deletion'];
export type SupervisionQueueItem = S['QueueItem'];
export type MeetingSupervision = S['MeetingSupervision'];
export type UserInfo = S['UserInfo'];
export type Shares = S['Shares'];
export type LocalModelStatus = S['LocalModelStatus'];
export type TeamStatus = S['TeamStatus'];
export type UserPrefs = S['UserPrefs-Output'];
export type UserInvite = S['Invite'];
export type Recording = S['Recording'];
export type ActionItem = S['ActionItem-Output'];
export type SummaryData = S['SummaryData'];
export type ClientFact = S['ClientFact-Output'];
export type SummaryEdit = S['SummaryEdit'];
export type TimedText = S['TimedText'];
export type Chapter = S['Chapter'];
export type Organization = S['Organization'];
export type OrganizationSummary = S['OrganizationSummary'];
export type OrganizationDetail = S['OrganizationDetail'];
export type OrganizationMember = S['OrganizationMember-Output'];
export type OrganizationFact = S['OrganizationFact'];
export type HubSpotSync = S['HubSpotSync'];
export type WordSegment = S['WordSegment-Output'];
export type TranscriptSegment = S['TranscriptSegment-Output'];
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
export type CommentHit = S['CommentHit'];
export type Citation = S['Citation'];
export type Ask = S['Ask'];
export type Digest = S['Digest'];
export type MeetingStats = S['MeetingStats'];
export type TaskItem = S['TaskItem'];
export type TaskObject = S['TaskObject'];
export type TaskMention = S['TaskMention'];
export type TaskSuggestion = S['TaskSuggestion'];
export type SessionLinks = S['SessionLinks'];
export type DecisionObject = S['DecisionObject'];
export type DecisionMention = S['DecisionMention'];
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
export type SyncStatus = S['SyncStatus'];
export type SyncPeerStatus = S['SyncPeerStatus'];
export type SyncPeerState = SyncPeerStatus['state'];
export type ContactInfo = S['ContactInfo'];
export type ContactsResponse = S['ContactsResponse'];
export type ContactsMe = S['ContactsMe'];
export type ContactPullInfo = S['ContactPullInfo'];
export type ShareParts = S['ShareParts-Output'];
export type PeerShareInfo = S['PeerShareInfo'];
export type ShareStatus = S['ShareStatus'];
export type ShareOwner = S['ShareOwner'];
export type ShareWaiting = S['ShareWaiting'];
export type ShareRejected = S['ShareRejected'];
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
export type HubSpotContact = S['HubSpotContact'];
export type HubSpotState = S['HubSpotState'];
export type HubSpotMatches = S['HubSpotMatches'];
export type HubSpotPushResult = S['HubSpotPushResult'];
export type Publication = S['Publication'];
export type GuestRoom = S['GuestRoom'];
export type PublicationSelection = S['PublicationSelection-Output'];
export type PublicationDefaults = S['PublicationDefaults'];
export type PublicationListItem = S['PublicationListItem'];
export type QuoteSpan = S['QuoteSpan-Output'];
/** The result of a finished `publication_file` job (backend routes/publishing.py export_file). */
export interface PublicationFileResult {
  token: string;
  filename: string;
  size: number;
}

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
  | { type: 'comments'; session_id: string; mentions: string[] } // a meeting's comments changed; mentions: user ids newly @named
  | { type: 'assets'; session_id: string } // a meeting's resources changed
  | { type: 'notes'; session_id: string } // a meeting's live notes were folded into Session.notes
  | { type: 'history'; session_id: string } // something was added to a meeting's history
  | { type: 'sync'; peer: string; state: SyncPeerState; done: number; total: number } // library sync with a peer
  | { type: 'share_outbox'; session_id: string; pending: number } // what I did on a meeting shared with me waits to be sent to its owner
  | { type: 'share_pull'; person: string; state: string; error: string | null } // pulling what a contact shares: ContactPullInfo.state
  | { type: 'shares' } // a meeting was shared or unshared (which one is not said: it may no longer be yours)
  | {
      // A source stopped being captured, or is captured again (backend audio/health.py).
      type: 'capture_health';
      session_id: string;
      device_id: number;
      state: 'stopped' | 'stalled' | 'ok';
      message: string;
    };

// ---- Live notes: /ws/notes/{session_id} (docs/api-reference.md "Live notes") -------------------
// Binary messages (0xDE delta, 0xAF artifact) go both ways and are not described here.

export interface NotesPerson {
  /** User id; "" on the desktop. */
  id: string;
  name: string;
}

/** A selection as element ids (64 hex characters; null = the start of the document). */
export interface NotesSel {
  a: string | null;
  f: string | null;
}

/** Someone in the room, as the relay lists them (never the connection itself, see `you.key`). */
export interface NotesMember {
  /** One per connection (`m` + 8 hex). */
  key: string;
  name: string;
  /** The user id when there is one, else the name: the caret colour is taken from it. */
  who: string;
  typing: boolean;
  sel: NotesSel | null;
}

/** A guest on the published notes link, as `guests.members` lists them. */
export interface NotesGuestMember {
  /** `g:` + the guest's id (the caret colour is taken from the id). */
  key: string;
  name: string;
  typing: boolean;
  sel: NotesSel | null;
}

export interface NotesHello {
  type: 'hello';
  /** sha256("mnemosyne/notes/" + session_id) in hex: the notes object's origin. */
  notes: string;
  you: NotesPerson & { /** This connection's key in `members` and `caret` messages. */ key: string };
  read_only: boolean;
  /** Set (with read_only) for a meeting whose document has not arrived by library sync yet. */
  waiting_for_sync?: boolean;
  present: string[];
  /** Everyone in the room now, you included. Absent from relays older than phase 11. */
  members?: NotesMember[];
  last_edit: { by: string; at: string } | null;
  /** One binary snapshot follows. */
  snapshot: boolean;
  /** This many binary frames follow the snapshot. */
  backlog: number;
}

/** No sidecar: use the plain-text notes route. The socket is closed (1000) after it. */
export interface NotesUnavailable {
  type: 'hello';
  unavailable: true;
}

/** JSON the notes relay sends. */
export type NotesServerMessage =
  | NotesHello
  | NotesUnavailable
  | { type: 'presence'; present: string[]; typing: string[]; members?: NotesMember[] }
  /** Someone else's selection moved (at most 10 a second per person; null = no selection). */
  | { type: 'caret'; key: string; sel: NotesSel | null }
  | { type: 'last_edit'; by: string; at: string }
  /** Guests (people on the published notes link) who are in the room or typing. */
  | { type: 'guests'; present: string[]; typing: string[]; members?: NotesGuestMember[] }
  /** A guest's selection moved (`key` is `g:<gid>`; null = no selection). */
  | { type: 'guest_caret'; key: string; sel: NotesSel | null }
  /** The stored document was replaced (library sync): rejoin to receive it. The close follows. */
  | { type: 'reload' }
  | { type: 'pong' };

/** JSON the client sends to the notes relay. */
export type NotesClientMessage =
  /** `typing` counts only from members that can edit, `sel` from everyone. Either may be left out. */
  | { type: 'presence'; typing?: boolean; sel?: NotesSel | null }
  | { type: 'ping' };
