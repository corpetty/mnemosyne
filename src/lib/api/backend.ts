import type {
  ActiveRecording,
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
  SystemInfo,
  Diagnostics,
  CopilotNotes,
  IssueResult,
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
  sessionId?: string
): Promise<StartRecordingResponse> {
  return request('/api/audio/start', {
    method: 'POST',
    body: JSON.stringify({ device_ids: deviceIds, session_id: sessionId ?? null })
  });
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

export async function deleteSession(sessionId: string): Promise<void> {
  return request(`/api/sessions/${sessionId}`, { method: 'DELETE' });
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

/** A one-time code (and phone page addresses carrying it) for pairing a phone. */
export async function createPairingCode(): Promise<PairingCode> {
  return request('/api/pairing/codes', { method: 'POST' });
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

export async function deleteSessionAudio(sessionId: string): Promise<SessionDetail> {
  return request(`/api/sessions/${sessionId}/audio`, { method: 'DELETE' });
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

export async function getIntegrations(): Promise<{ trackers: TrackerName[]; destinations: DestinationName[] }> {
  return request('/api/integrations');
}

export async function checkIntegration(name: 'linear' | 'jira' | 'slack' | 'matrix'): Promise<{ ok: boolean; message: string }> {
  return request(`/api/integrations/${name}/check`);
}

export async function sendFollowup(sessionId: string, destination: DestinationName, text: string): Promise<{ ok: boolean; message: string }> {
  return request(`/api/sessions/${sessionId}/followup/send`, {
    method: 'POST',
    body: JSON.stringify({ destination, text })
  });
}
