import type {
  ActiveRecording,
  ExternalNotes,
  Asset,
  LibraryAsset,
  AgendaItem,
  Bookmark,
  SessionHistory,
  AddCorrectionResult,
  GlossarySuggestion,
  BackupStatus,
  RestoreResponse,
  Ask,
  DesktopCalendar,
  EncryptionEnabled,
  EncryptionStatus,
  Quote,
  EchoCancelStatus,
  Digest,
  MeetingStats,
  TaskItem,
  Brief,
  IndexStatus,
  PersonSummary,
  PersonDetail,
  TopicCount,
  Thread,
  PhoneLink,
  PairingCode,
  PairedDevice,
  RemoteAccess,
  SystemInfo,
  Diagnostics,
  CopilotNotes,
  IssueResult,
  HubSpotMatches,
  HubSpotPushResult,
  HubSpotState,
  RepoCheck,
  Level,
  SelfTestResult,
  CalendarResponse,
  CleanupResult,
  AudioDevice,
  Job,
  ProviderModels,
  RecordingStatus,
  SearchHit,
  SessionDetail,
  SessionSpeaker,
  SessionSummary,
  Me,
  UserInfo,
  UserInvite,
  MeetingRecord,
  RecordVersion,
  Deletion,
  MeetingSupervision,
  Organization,
  OrganizationDetail,
  OrganizationSummary,
  HubSpotSync,
  SupervisionQueueItem,
  SettingsResponse,
  SettingsUpdate,
  SpeakerProfile,
  StartRecordingResponse,
  StopRecordingResponse,
  StorageReport,
  SummaryStyle
} from '$lib/types/index.js';

import { connectionState } from '$lib/stores/connection.svelte.js';

function base(): string {
  return connectionState.url;
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${base()}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...connectionState.headers(), ...(options?.headers ?? {}) }
  });
  if (!res.ok) {
    let detail = await res.text();
    try {
      detail = JSON.parse(detail).detail ?? detail;
    } catch {
      /* plain text */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

// Health
export async function getHealth(): Promise<{
  status: string;
  version: string;
  host?: string;
  auth_required?: boolean;
  team_mode?: boolean;
  cloud_models?: boolean;
  consent_required?: boolean;
}> {
  return request('/health');
}

// Devices
export async function getDevices(): Promise<AudioDevice[]> {
  return request('/api/devices');
}

// Audio
export async function startRecording(
  deviceIds: number[],
  sessionId?: string,
  consent?: string | null
): Promise<StartRecordingResponse> {
  return request('/api/audio/start', {
    method: 'POST',
    body: JSON.stringify({ device_ids: deviceIds, session_id: sessionId ?? null, consent: consent ?? null })
  });
}

/** Record from this browser (a team server): it then sends the audio itself
 * (app/browser-capture.ts). */
export async function startBrowserRecording(body: {
  sources: ('mic' | 'system')[];
  sample_rate: number;
  labels: Record<string, string>;
  session_id: string | null;
  consent: string | null;
}): Promise<StartRecordingResponse> {
  return request('/api/audio/start-browser', { method: 'POST', body: JSON.stringify(body) });
}

export async function stopRecording(
  sessionId: string,
  transcribe?: boolean
): Promise<StopRecordingResponse> {
  return request(`/api/audio/stop/${sessionId}`, {
    method: 'POST',
    body: JSON.stringify({ transcribe: transcribe ?? null })
  });
}

export async function getRecordingStatus(sessionId: string): Promise<RecordingStatus> {
  return request(`/api/audio/status/${sessionId}`);
}

/** Save what was recorded and go on as the next part of the meeting (a source failed). */
export async function restartRecording(sessionId: string): Promise<StartRecordingResponse> {
  return request(`/api/audio/restart/${sessionId}`, { method: 'POST' });
}

/** Recordings in progress: after a restart of the app, the backend may still be recording. */
export async function getActiveRecordings(): Promise<ActiveRecording[]> {
  return request('/api/audio/active');
}

// Sessions
export async function listSessions(): Promise<SessionSummary[]> {
  return request('/api/sessions');
}

export async function createSession(name?: string): Promise<SessionDetail> {
  return request('/api/sessions', {
    method: 'POST',
    body: JSON.stringify({ name: name ?? 'Untitled Session' })
  });
}

export async function getSession(sessionId: string): Promise<SessionDetail> {
  return request(`/api/sessions/${sessionId}`);
}

export async function renameSession(sessionId: string, name: string): Promise<SessionDetail> {
  return request(`/api/sessions/${sessionId}`, {
    method: 'PATCH',
    body: JSON.stringify({ name })
  });
}

/** `reason`: needed within the records period (backend services/records.py). */
export async function deleteSession(sessionId: string, reason = ''): Promise<void> {
  const q = reason ? `?reason=${encodeURIComponent(reason)}` : '';
  return request(`/api/sessions/${sessionId}${q}`, { method: 'DELETE' });
}

export async function updateNotes(sessionId: string, notes: string): Promise<SessionDetail> {
  return request(`/api/sessions/${sessionId}/notes`, {
    method: 'POST',
    body: JSON.stringify({ notes })
  });
}

/** `otherId` joins `sessionId` (its parts in recording order), then is deleted. A job. */
export async function combineSessions(sessionId: string, otherId: string): Promise<Job> {
  return request(`/api/sessions/${sessionId}/combine`, {
    method: 'POST',
    body: JSON.stringify({ other_id: otherId })
  });
}

export async function transcribeSession(sessionId: string): Promise<Job> {
  return request(`/api/sessions/${sessionId}/transcribe`, { method: 'POST' });
}

// Jobs
export async function listJobs(activeOnly = false): Promise<Job[]> {
  return request(`/api/jobs?active_only=${activeOnly}`);
}

// Notes from other assistants (Gemini in Google Meet, Zoom, Otter, Teams Copilot...)
export async function addExternalNotes(sessionId: string, text: string, source = ''): Promise<ExternalNotes> {
  return request(`/api/sessions/${sessionId}/external-notes`, { method: 'POST', body: JSON.stringify({ text, source }) });
}

export async function uploadExternalNotes(sessionId: string, file: File, source = ''): Promise<ExternalNotes> {
  const form = new FormData();
  form.append('file', file, file.name);
  if (source) form.append('source', source);
  const res = await fetch(`${base()}/api/sessions/${sessionId}/external-notes/file`, {
    method: 'POST',
    body: form,
    headers: connectionState.headers()
  });
  if (!res.ok) {
    let detail = await res.text();
    try {
      detail = JSON.parse(detail).detail ?? detail;
    } catch {
      /* plain text */
    }
    throw new Error(detail);
  }
  return res.json();
}

export async function deleteExternalNotes(sessionId: string, notesId: string): Promise<void> {
  await request(`/api/sessions/${sessionId}/external-notes/${notesId}`, { method: 'DELETE' });
}

// Resources (links and files) for meetings, from a shared library
export async function listAssets(q = ''): Promise<LibraryAsset[]> {
  return request(`/api/assets?q=${encodeURIComponent(q)}`);
}

export async function addLink(url: string, title = '', sessionId?: string): Promise<Asset> {
  return request('/api/assets/link', { method: 'POST', body: JSON.stringify({ url, title, session_id: sessionId ?? null }) });
}

export async function uploadAsset(file: File, sessionId?: string): Promise<Asset> {
  const form = new FormData();
  form.append('file', file, file.name);
  if (sessionId) form.append('session_id', sessionId);
  const res = await fetch(`${base()}/api/assets/file`, { method: 'POST', body: form, headers: connectionState.headers() });
  if (!res.ok) {
    let detail = await res.text();
    try {
      detail = JSON.parse(detail).detail ?? detail;
    } catch {
      /* plain text */
    }
    throw new Error(detail);
  }
  return res.json();
}

export async function attachAsset(sessionId: string, assetId: string): Promise<Asset[]> {
  return request(`/api/sessions/${sessionId}/assets`, { method: 'POST', body: JSON.stringify({ asset_id: assetId }) });
}

export async function detachAsset(sessionId: string, assetId: string): Promise<Asset[]> {
  return request(`/api/sessions/${sessionId}/assets/${assetId}`, { method: 'DELETE' });
}

export function assetFileUrl(assetId: string): string {
  return connectionState.withToken(`${base()}/api/assets/${assetId}/file`);
}

/** Choose a meeting's type (a name from settings.meeting_types, or "none"). */
export async function setMeetingType(sessionId: string, name: string): Promise<SessionDetail> {
  return request(`/api/sessions/${sessionId}/meeting-type`, { method: 'PUT', body: JSON.stringify({ name }) });
}

/** A meeting's agenda, in order (the copilot marks points covered as they come up). */
export async function setAgenda(sessionId: string, items: AgendaItem[]): Promise<AgendaItem[]> {
  return request(`/api/sessions/${sessionId}/agenda`, { method: 'PUT', body: JSON.stringify({ items }) });
}

/** A meeting's parts, files, audio waiting to be saved, and log (services/history.py). */
export async function getHistory(sessionId: string): Promise<SessionHistory> {
  return request(`/api/sessions/${sessionId}/history`);
}

/** Save audio recorded but not in the meeting yet (an interrupted recording) as its next part. */
export async function recoverSession(sessionId: string): Promise<Job> {
  return request(`/api/sessions/${sessionId}/recover`, { method: 'POST' });
}

/** Mark a moment of a meeting: `at` seconds on its timeline, or (while recording) now. */
export async function addBookmark(sessionId: string, at?: number, note = ''): Promise<Bookmark> {
  return request(`/api/sessions/${sessionId}/bookmarks`, {
    method: 'POST',
    body: JSON.stringify({ at: at ?? null, note })
  });
}

export async function updateBookmark(sessionId: string, id: string, note: string): Promise<Bookmark> {
  return request(`/api/sessions/${sessionId}/bookmarks/${id}`, { method: 'PATCH', body: JSON.stringify({ note }) });
}

export async function deleteBookmark(sessionId: string, id: string): Promise<void> {
  await request(`/api/sessions/${sessionId}/bookmarks/${id}`, { method: 'DELETE' });
}

/** Glossary entries a transcript correction suggests (a misheard name, product, acronym). */
export async function suggestGlossary(before: string, after: string): Promise<GlossarySuggestion[]> {
  return request('/api/glossary/suggest', { method: 'POST', body: JSON.stringify({ before, after }) });
}

/** Add `heard -> correct` to the glossary; with a session, also fix the rest of its transcript. */
export async function addGlossaryCorrection(
  heard: string,
  correct: string,
  sessionId?: string
): Promise<AddCorrectionResult> {
  return request('/api/glossary/corrections', {
    method: 'POST',
    body: JSON.stringify({ heard, correct, session_id: sessionId ?? null })
  });
}

export async function getJob(jobId: string): Promise<Job> {
  return request(`/api/jobs/${jobId}`);
}

export async function cancelJob(jobId: string): Promise<Job> {
  return request(`/api/jobs/${jobId}/cancel`, { method: 'POST' });
}

// Models & Summarization
export async function listModels(): Promise<ProviderModels[]> {
  return request('/api/models');
}

export async function summarizeSession(
  sessionId: string,
  provider = '',
  model = '',
  style = ''
): Promise<Job> {
  return request(`/api/sessions/${sessionId}/summarize`, {
    method: 'POST',
    body: JSON.stringify({ provider, model, style })
  });
}

export async function listSummaryStyles(): Promise<SummaryStyle[]> {
  return request('/api/summary-styles');
}

export async function getExportMarkdown(sessionId: string): Promise<{ markdown: string }> {
  return request(`/api/sessions/${sessionId}/export/markdown`);
}

// Export
export async function exportToObsidian(
  sessionId: string
): Promise<{ path: string; message: string }> {
  return request(`/api/sessions/${sessionId}/export/obsidian`, { method: 'POST' });
}

// Settings
export async function getSettings(): Promise<SettingsResponse> {
  return request('/api/settings');
}

export async function updateSettings(update: SettingsUpdate): Promise<SettingsResponse> {
  return request('/api/settings', { method: 'PUT', body: JSON.stringify(update) });
}

// Speakers
export async function getSessionSpeakers(sessionId: string): Promise<SessionSpeaker[]> {
  return request(`/api/sessions/${sessionId}/speakers`);
}

export async function renameSessionSpeaker(
  sessionId: string,
  label: string,
  name: string,
  enroll = true
): Promise<SessionDetail> {
  return request(`/api/sessions/${sessionId}/speakers/rename`, {
    method: 'POST',
    body: JSON.stringify({ label, name, enroll })
  });
}

export async function makeQuote(
  sessionId: string,
  firstIdx: number,
  lastIdx: number,
  audio: boolean
): Promise<Quote> {
  return request(`/api/sessions/${sessionId}/clip`, {
    method: 'POST',
    body: JSON.stringify({ first_idx: firstIdx, last_idx: lastIdx, audio })
  });
}

export function clipUrl(sessionId: string, clipId: string): string {
  return connectionState.withToken(`${base()}/api/sessions/${sessionId}/clips/${clipId}`);
}

export async function saveClip(sessionId: string, clipId: string, path: string): Promise<{ path: string }> {
  return request(`/api/sessions/${sessionId}/clips/${clipId}/save`, {
    method: 'POST',
    body: JSON.stringify({ path })
  });
}

// Encryption at rest
// ---- people on a team server (backend services/users.py) ------------------------------

export async function getMe(): Promise<Me> {
  return request('/api/users/me');
}

export async function redeemInvite(code: string, device: string): Promise<{ token: string; user: UserInfo }> {
  return request('/api/users/redeem', { method: 'POST', body: JSON.stringify({ code, device }) });
}

export async function signOut(): Promise<void> {
  await request('/api/users/me/signout', { method: 'POST' });
}

export async function listUsers(): Promise<UserInfo[]> {
  return request('/api/users');
}

export async function addUser(name: string, email: string, role: string): Promise<UserInvite> {
  return request('/api/users', { method: 'POST', body: JSON.stringify({ name, email, role }) });
}

export async function changeUser(
  id: string,
  change: { name?: string; email?: string; role?: string; disabled?: boolean }
): Promise<UserInfo> {
  return request(`/api/users/${id}`, { method: 'PATCH', body: JSON.stringify(change) });
}

export async function inviteUser(id: string): Promise<UserInvite> {
  return request(`/api/users/${id}/invite`, { method: 'POST' });
}

export async function signOutUser(id: string): Promise<void> {
  await request(`/api/users/${id}/signout`, { method: 'POST' });
}

export async function getEncryption(): Promise<EncryptionStatus> {
  return request('/api/encryption');
}

export async function enableEncryption(): Promise<EncryptionEnabled> {
  return request('/api/encryption/enable', { method: 'POST' });
}

export async function disableEncryption(): Promise<EncryptionStatus> {
  return request('/api/encryption/disable', { method: 'POST' });
}

export async function unlockEncryption(recoveryCode: string): Promise<EncryptionStatus> {
  return request('/api/encryption/unlock', {
    method: 'POST',
    body: JSON.stringify({ recovery_code: recoveryCode })
  });
}

export async function setSpeakersReviewed(sessionId: string, reviewed = true): Promise<SessionDetail> {
  return request(`/api/sessions/${sessionId}/speakers/reviewed`, {
    method: 'POST',
    body: JSON.stringify({ reviewed })
  });
}

export async function listSpeakers(): Promise<SpeakerProfile[]> {
  return request('/api/speakers');
}

export async function renameSpeakerProfile(id: string, name: string): Promise<SpeakerProfile> {
  return request(`/api/speakers/${id}`, { method: 'PATCH', body: JSON.stringify({ name }) });
}

export async function deleteSpeakerProfile(id: string): Promise<void> {
  return request(`/api/speakers/${id}`, { method: 'DELETE' });
}

// Transcript editing
export async function updateSegment(
  sessionId: string,
  idx: number,
  update: { text?: string; speaker?: string }
): Promise<SessionDetail> {
  return request(`/api/sessions/${sessionId}/segments/${idx}`, {
    method: 'PATCH',
    body: JSON.stringify(update)
  });
}

export async function deleteSegment(sessionId: string, idx: number): Promise<SessionDetail> {
  return request(`/api/sessions/${sessionId}/segments/${idx}`, { method: 'DELETE' });
}

export async function mergeSegmentUp(sessionId: string, idx: number): Promise<SessionDetail> {
  return request(`/api/sessions/${sessionId}/segments/${idx}/merge`, { method: 'POST' });
}

export async function splitSegment(
  sessionId: string,
  idx: number,
  offset: number
): Promise<SessionDetail> {
  return request(`/api/sessions/${sessionId}/segments/${idx}/split`, {
    method: 'POST',
    body: JSON.stringify({ offset })
  });
}

// Search
export async function search(q: string, limit = 20): Promise<SearchHit[]> {
  return request(`/api/search?q=${encodeURIComponent(q)}&limit=${limit}`);
}

// Playback & import
export function audioUrl(sessionId: string, recordingId?: string): string {
  const q = recordingId ? `?recording=${encodeURIComponent(recordingId)}` : '';
  return connectionState.withToken(`${base()}/api/audio/file/${sessionId}${q}`);
}

export async function importAudio(
  file: File,
  opts: { name?: string; transcribe?: boolean; sessionId?: string } = {}
): Promise<StopRecordingResponse> {
  const form = new FormData();
  form.append('file', file, file.name);
  if (opts.name) form.append('name', opts.name);
  if (opts.sessionId) form.append('session_id', opts.sessionId); // added as the meeting's next part
  if (opts.transcribe === false) form.append('transcribe', 'false');
  const res = await fetch(`${base()}/api/audio/import`, {
    method: 'POST',
    body: form,
    headers: connectionState.headers()
  });
  if (!res.ok) {
    let detail = await res.text();
    try {
      detail = JSON.parse(detail).detail ?? detail;
    } catch {
      /* plain text */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

// Echo cancellation
export type { EchoCancelStatus };

export async function getEchoCancel(): Promise<EchoCancelStatus> {
  return request('/api/audio/echo-cancel');
}

/** `mic`: the microphone's node name to cancel echo on ("" = the default source; omitted = keep). */
export async function setEchoCancel(enabled: boolean, mic?: string): Promise<EchoCancelStatus> {
  return request('/api/audio/echo-cancel', { method: 'POST', body: JSON.stringify({ enabled, mic }) });
}

// Ask across meetings
export async function askQuestion(question: string, provider = '', model = ''): Promise<Job> {
  return request('/api/ask', { method: 'POST', body: JSON.stringify({ question, provider, model }) });
}

export async function listAsks(limit = 50): Promise<Ask[]> {
  return request(`/api/asks?limit=${limit}`);
}

export async function deleteAsk(id: string): Promise<void> {
  return request(`/api/asks/${id}`, { method: 'DELETE' });
}

export async function getSessionStats(id: string): Promise<MeetingStats> {
  return request(`/api/sessions/${id}/stats`);
}

export async function draftFollowup(sessionId: string, style: 'email' | 'chat'): Promise<Job> {
  return request(`/api/sessions/${sessionId}/followup`, { method: 'POST', body: JSON.stringify({ style }) });
}

export async function getBrief(title: string, attendees: string[], exclude?: string): Promise<Brief> {
  const q = new URLSearchParams({ title });
  for (const a of attendees) q.append('attendees', a);
  if (exclude) q.set('exclude', exclude);
  return request(`/api/brief?${q}`);
}

export async function getIndexStatus(): Promise<IndexStatus> {
  return request('/api/search/index');
}

export async function rebuildIndex(): Promise<IndexStatus> {
  return request('/api/search/index/rebuild', { method: 'POST' });
}

export async function listPeople(): Promise<PersonSummary[]> {
  return request('/api/people');
}

export async function getPerson(name: string): Promise<PersonDetail> {
  return request(`/api/people/${encodeURIComponent(name)}`);
}

export async function listOrganizations(): Promise<OrganizationSummary[]> {
  return request('/api/organizations');
}

export async function getOrganization(id: string): Promise<OrganizationDetail> {
  return request(`/api/organizations/${id}`);
}

export async function saveOrganization(
  body: { name: string; members: { name: string; email?: string; source?: 'manual' | 'hubspot' }[] },
  id?: string
): Promise<Organization> {
  return request(id ? `/api/organizations/${id}` : '/api/organizations', {
    method: id ? 'PUT' : 'POST',
    body: JSON.stringify(body)
  });
}

export async function deleteOrganization(id: string): Promise<void> {
  await request(`/api/organizations/${id}`, { method: 'DELETE' });
}

export async function syncOrganizationsFromHubSpot(): Promise<HubSpotSync> {
  return request('/api/organizations/sync-hubspot', { method: 'POST' });
}

export async function listTopics(): Promise<TopicCount[]> {
  return request('/api/topics');
}

export async function getThread(q: string): Promise<Thread> {
  return request(`/api/topics/thread?q=${encodeURIComponent(q)}`);
}

export async function summarizeThread(q: string): Promise<Job> {
  return request('/api/topics/thread/summary', { method: 'POST', body: JSON.stringify({ q }) });
}

export async function setLocalOnly(sessionId: string, localOnly: boolean): Promise<SessionDetail> {
  return request(`/api/sessions/${sessionId}/local-only`, {
    method: 'PUT',
    body: JSON.stringify({ local_only: localOnly })
  });
}

export async function getPhoneLink(): Promise<PhoneLink> {
  return request('/api/server/phone');
}

/** A one-time pairing code: for a phone, the phone page addresses carrying it; for a computer,
 * the invite to paste on it (needs remote access running). */
export async function createPairingCode(kind: 'phone' | 'desktop' = 'phone'): Promise<PairingCode> {
  return request('/api/pairing/codes', { method: 'POST', body: JSON.stringify({ kind }) });
}

export async function getRemoteAccess(): Promise<RemoteAccess> {
  return request('/api/pairing/remote');
}

export async function listPairedDevices(): Promise<PairedDevice[]> {
  return request('/api/pairing/devices');
}

export async function removePairedDevice(id: string): Promise<void> {
  await request(`/api/pairing/devices/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

export async function getSystemInfo(): Promise<SystemInfo> {
  return request('/api/system');
}

export async function getDiagnostics(): Promise<Diagnostics> {
  return request('/api/system/diagnostics');
}

export async function getCopilotNotes(sessionId: string): Promise<CopilotNotes | null> {
  return request(`/api/sessions/${sessionId}/copilot`);
}

export async function askCopilot(sessionId: string, question: string): Promise<Job> {
  return request(`/api/sessions/${sessionId}/copilot/ask`, { method: 'POST', body: JSON.stringify({ question }) });
}

// Action items across meetings
export async function listActionItems(status: 'open' | 'done' | 'all' = 'open'): Promise<TaskItem[]> {
  return request(`/api/action-items?status=${status}`);
}

export async function setActionItemDone(sessionId: string, idx: number, done: boolean): Promise<TaskItem> {
  return request(`/api/sessions/${sessionId}/action-items/${idx}`, {
    method: 'PATCH',
    body: JSON.stringify({ done })
  });
}

// Digests
export async function createDigest(start: string, end: string, provider = '', model = ''): Promise<Job> {
  return request('/api/digests', { method: 'POST', body: JSON.stringify({ start, end, provider, model }) });
}

export async function listDigests(limit = 50): Promise<Digest[]> {
  return request(`/api/digests?limit=${limit}`);
}

export async function deleteDigest(id: string): Promise<void> {
  return request(`/api/digests/${id}`, { method: 'DELETE' });
}

// Storage
export async function getStorage(): Promise<StorageReport> {
  return request('/api/storage');
}

// Backups
export async function getBackups(): Promise<BackupStatus> {
  return request('/api/backup');
}

export async function backUpNow(): Promise<Job> {
  return request('/api/backup', { method: 'POST' });
}

/** Stage a restore (a backup in the backup folder, by name); the backend applies it when it
 *  next starts. */
export async function restoreBackup(name: string): Promise<RestoreResponse> {
  return request('/api/backup/restore', { method: 'POST', body: JSON.stringify({ name }) });
}

export async function deleteSessionAudio(sessionId: string, reason = ''): Promise<SessionDetail> {
  const q = reason ? `?reason=${encodeURIComponent(reason)}` : '';
  return request(`/api/sessions/${sessionId}/audio${q}`, { method: 'DELETE' });
}

// ---- records (backend services/records.py) --------------------------------------------

export async function getMeetingRecord(sessionId: string): Promise<MeetingRecord> {
  return request(`/api/sessions/${sessionId}/records`);
}

export async function getRecordVersion(sessionId: string, versionId: number): Promise<RecordVersion> {
  return request(`/api/sessions/${sessionId}/versions/${versionId}`);
}

export async function setLegalHold(sessionId: string, reason: string): Promise<MeetingRecord> {
  return request(`/api/sessions/${sessionId}/legal-hold`, { method: 'PUT', body: JSON.stringify({ reason }) });
}

export async function getSupervisionQueue(): Promise<SupervisionQueueItem[]> {
  return request('/api/supervision');
}

export async function getMeetingSupervision(sessionId: string): Promise<MeetingSupervision> {
  return request(`/api/sessions/${sessionId}/supervision`);
}

export async function markReviewed(sessionId: string, note: string): Promise<MeetingSupervision> {
  return request(`/api/sessions/${sessionId}/supervision/review`, { method: 'POST', body: JSON.stringify({ note }) });
}

export async function scanForFlaggedPhrases(): Promise<Job> {
  return request('/api/supervision/scan', { method: 'POST' });
}

export async function startRecordsExport(body: {
  session_ids?: string[];
  start?: string | null;
  end?: string | null;
}): Promise<Job> {
  return request('/api/records/export', { method: 'POST', body: JSON.stringify(body) });
}

/** Where to download an export (once), token included for a plain link. */
export function recordsExportUrl(exportId: string): string {
  return connectionState.withToken(`${base()}/api/records/exports/${exportId}`);
}

export async function getDeletions(start = '', end = ''): Promise<Deletion[]> {
  const q = new URLSearchParams();
  if (start) q.set('start', start);
  if (end) q.set('end', end);
  return request(`/api/records/deletions?${q}`);
}

export async function runCleanup(days: number, dryRun: boolean): Promise<CleanupResult> {
  return request(`/api/storage/cleanup?days=${days}&dry_run=${dryRun}`, { method: 'POST' });
}

// Calendar
export async function listDesktopCalendars(): Promise<DesktopCalendar[]> {
  return request('/api/calendar/desktop');
}

export async function getCalendar(hours = 12, refresh = false): Promise<CalendarResponse> {
  return request(`/api/calendar?hours=${hours}&refresh=${refresh}`);
}

export async function setAttendees(sessionId: string, attendees: string[]): Promise<SessionDetail> {
  return request(`/api/sessions/${sessionId}/attendees`, {
    method: 'PUT',
    body: JSON.stringify({ attendees })
  });
}

// Levels and the system-audio self-test
export async function getDeviceLevel(deviceId: number, seconds = 1): Promise<Level> {
  return request(`/api/audio/level/${deviceId}?seconds=${seconds}`);
}

export async function runSelfTest(deviceId: number): Promise<SelfTestResult> {
  return request('/api/audio/self-test', { method: 'POST', body: JSON.stringify({ device_id: deviceId }) });
}

// GitHub issues from action items
export async function checkGitHub(): Promise<RepoCheck> {
  return request('/api/integrations/github/check');
}

export type TrackerName = 'github' | 'linear' | 'jira';
export type DestinationName = 'slack' | 'matrix';

export async function createIssues(sessionId: string, tracker: TrackerName, indices: number[]): Promise<IssueResult> {
  return request(`/api/sessions/${sessionId}/action-items/${tracker}`, {
    method: 'POST',
    body: JSON.stringify({ indices })
  });
}

export async function getIntegrations(): Promise<{ trackers: TrackerName[]; destinations: DestinationName[]; crm: 'hubspot'[] }> {
  return request('/api/integrations');
}

export async function checkIntegration(name: 'linear' | 'jira' | 'slack' | 'matrix' | 'hubspot'): Promise<{ ok: boolean; message: string }> {
  return request(`/api/integrations/${name}/check`);
}

// HubSpot: the meeting, a note and its action items as tasks, on the confirmed contacts
export async function getHubSpotState(sessionId: string): Promise<HubSpotState> {
  return request(`/api/sessions/${sessionId}/crm/hubspot`);
}

export async function getHubSpotMatches(sessionId: string): Promise<HubSpotMatches> {
  return request(`/api/sessions/${sessionId}/crm/hubspot/matches`);
}

export async function pushToHubSpot(sessionId: string, contactIds: string[]): Promise<HubSpotPushResult> {
  return request(`/api/sessions/${sessionId}/crm/hubspot`, {
    method: 'POST',
    body: JSON.stringify({ contact_ids: contactIds })
  });
}

export async function sendFollowup(sessionId: string, destination: DestinationName, text: string): Promise<{ ok: boolean; message: string }> {
  return request(`/api/sessions/${sessionId}/followup/send`, {
    method: 'POST',
    body: JSON.stringify({ destination, text })
  });
}
