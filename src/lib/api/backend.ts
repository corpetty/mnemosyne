import type {
  AudioDevice,
  StartRecordingResponse,
  StopRecordingResponse,
  RecordingStatus,
  SessionSummary,
  SessionDetail,
  ProviderModels,
  SummarizeResponse,
} from "$lib/types/index.js";

const BASE_URL = "http://127.0.0.1:8008";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

// Health
export async function getHealth(): Promise<{ status: string }> {
  return request("/health");
}

// Devices
export async function getDevices(): Promise<AudioDevice[]> {
  return request("/api/devices");
}

// Audio
export async function startRecording(
  deviceIds: number[],
  sessionId?: string,
): Promise<StartRecordingResponse> {
  return request("/api/audio/start", {
    method: "POST",
    body: JSON.stringify({
      device_ids: deviceIds,
      session_id: sessionId ?? null,
    }),
  });
}

export async function stopRecording(
  sessionId: string,
): Promise<StopRecordingResponse> {
  return request(`/api/audio/stop/${sessionId}`, { method: "POST" });
}

export async function getRecordingStatus(
  sessionId: string,
): Promise<RecordingStatus> {
  return request(`/api/audio/status/${sessionId}`);
}

// Sessions
export async function listSessions(): Promise<SessionSummary[]> {
  return request("/api/sessions");
}

export async function createSession(name?: string): Promise<SessionDetail> {
  return request("/api/sessions", {
    method: "POST",
    body: JSON.stringify({ name: name ?? "Untitled Session" }),
  });
}

export async function getSession(sessionId: string): Promise<SessionDetail> {
  return request(`/api/sessions/${sessionId}`);
}

export async function renameSession(
  sessionId: string,
  name: string,
): Promise<SessionDetail> {
  return request(`/api/sessions/${sessionId}`, {
    method: "PATCH",
    body: JSON.stringify({ name }),
  });
}

export async function deleteSession(sessionId: string): Promise<void> {
  return request(`/api/sessions/${sessionId}`, { method: "DELETE" });
}

export async function updateNotes(
  sessionId: string,
  notes: string,
): Promise<SessionDetail> {
  return request(`/api/sessions/${sessionId}/notes`, {
    method: "POST",
    body: JSON.stringify({ notes }),
  });
}

// Models & Summarization
export async function listModels(): Promise<ProviderModels[]> {
  return request("/api/models");
}

export async function summarizeSession(
  sessionId: string,
  provider: string = "ollama",
  model: string = "",
): Promise<SummarizeResponse> {
  return request(`/api/sessions/${sessionId}/summarize`, {
    method: "POST",
    body: JSON.stringify({ provider, model }),
  });
}

// Obsidian export
export async function exportToObsidian(
  sessionId: string,
): Promise<{ path: string; message: string }> {
  return request(`/api/sessions/${sessionId}/export/obsidian`, {
    method: "POST",
  });
}

export async function getVaultConfig(): Promise<{
  vault_path: string;
  subfolder: string;
  exists: boolean;
}> {
  return request("/api/settings/obsidian");
}

export async function setVaultConfig(
  vaultPath: string,
  subfolder: string = "meetings/mnemosyne",
): Promise<{ vault_path: string; subfolder: string; exists: boolean }> {
  return request("/api/settings/obsidian", {
    method: "POST",
    body: JSON.stringify({ vault_path: vaultPath, subfolder }),
  });
}
