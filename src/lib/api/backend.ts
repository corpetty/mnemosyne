import type {
  AudioDevice,
  Job,
  ProviderModels,
  RecordingStatus,
  SessionDetail,
  SessionSpeaker,
  SessionSummary,
  SettingsResponse,
  SettingsUpdate,
  SpeakerProfile,
  StartRecordingResponse,
  StopRecordingResponse,
  SummarizeResponse
} from '$lib/types/index.js';

const BASE_URL = 'http://127.0.0.1:8008';

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options
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
export async function getHealth(): Promise<{ status: string; version: string }> {
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

export async function transcribeSession(sessionId: string): Promise<Job> {
  return request(`/api/sessions/${sessionId}/transcribe`, { method: 'POST' });
}

// Jobs
export async function listJobs(activeOnly = false): Promise<Job[]> {
  return request(`/api/jobs?active_only=${activeOnly}`);
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
  model = ''
): Promise<SummarizeResponse> {
  return request(`/api/sessions/${sessionId}/summarize`, {
    method: 'POST',
    body: JSON.stringify({ provider, model })
  });
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

export async function listSpeakers(): Promise<SpeakerProfile[]> {
  return request('/api/speakers');
}

export async function renameSpeakerProfile(id: string, name: string): Promise<SpeakerProfile> {
  return request(`/api/speakers/${id}`, { method: 'PATCH', body: JSON.stringify({ name }) });
}

export async function deleteSpeakerProfile(id: string): Promise<void> {
  return request(`/api/speakers/${id}`, { method: 'DELETE' });
}
