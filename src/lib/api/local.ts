/**
 * This computer's OWN backend (http://127.0.0.1:8008, with its own token), for the window of a
 * computer that uses a main computer: its main connection (api/backend.ts) points at the main
 * computer through remote access, but recording, the outbox and this computer's audio devices
 * are the local backend's (backend api/routes/satellite.py; docs/plans/2026-11-09-connections-
 * design.md, task 8). Nothing here ever goes to the main computer.
 */
import { connectionState, LOCAL_BACKEND } from '$lib/stores/connection.svelte.js';
import type {
  AudioDevice,
  EchoCancelStatus,
  Level,
  OutboxEntry,
  PrepareResult,
  SatelliteStartResponse,
  SatelliteStatus,
  SatelliteStopResponse,
  SelfTestResult,
  SyncStatus
} from '$lib/types/index.js';

export async function localRequest<T>(path: string, options?: RequestInit): Promise<T> {
  const token = connectionState.localToken;
  const res = await fetch(`${LOCAL_BACKEND}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options?.headers ?? {})
    }
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

// Recording through this computer (the satellite routes)
export const getSatellite = (): Promise<SatelliteStatus> => localRequest('/api/satellite');

/** Where a recording goes: the main computer this one uses, or the team it joined (the backend
 * answers 409 when this computer has no such destination). Left out = the main computer. */
export type SatelliteDestination = 'main' | 'team';

export interface SatelliteStart {
  mic_device: number | null;
  system_device: number | null;
  title?: string;
  consent?: 'all_parties' | 'in_person' | 'one_party' | null;
  destination?: SatelliteDestination;
}

export const startSatellite = (body: SatelliteStart): Promise<SatelliteStartResponse> =>
  localRequest('/api/satellite/start', { method: 'POST', body: JSON.stringify(body) });

export const stopSatellite = (notes?: string | null): Promise<SatelliteStopResponse> =>
  localRequest('/api/satellite/stop', { method: 'POST', body: JSON.stringify({ notes: notes ?? null }) });

export const outboxAsNewMeeting = (id: string): Promise<OutboxEntry> =>
  localRequest(`/api/satellite/outbox/${encodeURIComponent(id)}/new-meeting`, { method: 'POST' });

export const outboxDiscard = (id: string): Promise<{ id: string }> =>
  localRequest(`/api/satellite/outbox/${encodeURIComponent(id)}`, { method: 'DELETE' });

// This computer's audio devices (the main computer's are not the ones to record from)
export const getLocalDevices = (): Promise<AudioDevice[]> => localRequest('/api/devices');
export const getLocalDeviceLevel = (id: number, seconds = 1): Promise<Level> =>
  localRequest(`/api/audio/level/${id}?seconds=${seconds}`);
export const runLocalSelfTest = (id: number): Promise<SelfTestResult> =>
  localRequest('/api/audio/self-test', { method: 'POST', body: JSON.stringify({ device_id: id }) });
export const getLocalEchoCancel = (): Promise<EchoCancelStatus> => localRequest('/api/audio/echo-cancel');
export const setLocalEchoCancel = (
  enabled: boolean,
  mic?: string,
  noise?: { noise_suppression?: boolean; noise_suppression_db?: number; noise_suppression_record?: boolean }
): Promise<EchoCancelStatus> =>
  localRequest('/api/audio/echo-cancel', { method: 'POST', body: JSON.stringify({ enabled, mic, ...noise }) });

// A copy of the main computer's library on this computer: library sync, paired by this
// computer's own backend (Settings → This computer → Uses my main computer).
export const getLocalSyncStatus = (): Promise<SyncStatus> => localRequest('/api/sync/status');

/** Redeem a sync invite from the main computer with THIS computer's backend. The backend makes
 * its API token first when it has none (pairing prepare); the window keeps that token. */
export async function localSyncPair(invite: string, name = ''): Promise<{ peer: { id: string; name: string } }> {
  const r = await localRequest<PrepareResult>('/api/pairing/prepare', {
    method: 'POST',
    body: JSON.stringify({ kind: 'sync' })
  });
  if (r.new_api_token) connectionState.setLocalToken(r.new_api_token);
  return localRequest('/api/sync/pair', { method: 'POST', body: JSON.stringify({ invite, name }) });
}

export const localRemoveSyncPeer = (id: string): Promise<unknown> =>
  localRequest(`/api/sync/peers/${encodeURIComponent(id)}`, { method: 'DELETE' });

export const localSyncNow = (): Promise<unknown> => localRequest('/api/sync/now', { method: 'POST' });
