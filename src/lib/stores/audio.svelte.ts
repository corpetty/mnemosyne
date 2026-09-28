import type { AudioDevice, BackendEvent, Level } from "$lib/types/index.js";
import * as api from "$lib/api/backend.js";
import { wsState } from "./websocket.svelte.js";

// Remembered by PipeWire node name: numeric ids change between restarts.
const SELECTION_KEY = 'mnemosyne.selectedDevices';

function loadSavedNames(): string[] {
  try {
    const raw = localStorage.getItem(SELECTION_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((x) => typeof x === 'string') : [];
  } catch {
    return [];
  }
}

class AudioState {
  devices = $state<AudioDevice[]>([]);
  selectedDeviceIds = $state<Set<number>>(new Set());
  isRecording = $state(false);
  /** A click on Record / Stop waiting for the backend: shown at once, so the click registers. */
  pending = $state<'starting' | 'stopping' | null>(null);
  activeSessionId = $state<string | null>(null);
  recordingDuration = $state(0);
  error = $state<string | null>(null);
  loading = $state(false);
  /** Sources not being captured, by device id: 'stopped' or 'stalled' (`capture_health`). */
  problems = $state<Record<string, string>>({});
  /** Live input levels per device id while recording (from `levels` events). */
  levels = $state<Record<string, Level>>({});
  private unsubscribeLevels: (() => void) | null = null;

  listenForLevels() {
    this.unsubscribeLevels?.();
    this.unsubscribeLevels = wsState.onMessage((raw) => {
      const msg = raw as BackendEvent;
      if (msg.type === 'levels' && msg.session_id === this.activeSessionId) this.levels = msg.levels;
      if (msg.type === 'capture_health' && msg.session_id === this.activeSessionId) {
        const next = { ...this.problems };
        if (msg.state === 'ok') delete next[String(msg.device_id)];
        else next[String(msg.device_id)] = msg.state;
        this.problems = next;
      }
    });
  }

  private durationInterval: ReturnType<typeof setInterval> | null = null;

  get inputDevices(): AudioDevice[] {
    return this.devices.filter((d) => d.is_input);
  }

  get outputDevices(): AudioDevice[] {
    return this.devices.filter((d) => d.is_output);
  }

  async loadDevices() {
    this.loading = true;
    this.error = null;
    try {
      this.devices = await api.getDevices();
      this.restoreSelection();
    } catch (e) {
      this.error = e instanceof Error ? e.message : "Failed to load devices";
    } finally {
      this.loading = false;
    }
  }

  toggleDevice(deviceId: number) {
    const next = new Set(this.selectedDeviceIds);
    if (next.has(deviceId)) {
      next.delete(deviceId);
    } else {
      next.add(deviceId);
    }
    this.selectedDeviceIds = next;
    this.saveSelection();
  }

  /** Keep the current selection where it still exists; otherwise restore the saved one. */
  private restoreSelection() {
    const present = new Set(this.devices.map((d) => d.id));
    const kept = [...this.selectedDeviceIds].filter((id) => present.has(id));
    if (kept.length) {
      this.selectedDeviceIds = new Set(kept);
      return;
    }
    const names = new Set(loadSavedNames());
    this.selectedDeviceIds = new Set(this.devices.filter((d) => names.has(d.name)).map((d) => d.id));
  }

  private saveSelection() {
    const names = this.devices.filter((d) => this.selectedDeviceIds.has(d.id)).map((d) => d.name);
    try {
      localStorage.setItem(SELECTION_KEY, JSON.stringify(names));
    } catch {
      /* no storage */
    }
  }

  /** Make sure devices are loaded and a remembered selection is applied. */
  async ensureDevices() {
    if (this.devices.length === 0) await this.loadDevices();
    return this.selectedDeviceIds.size > 0;
  }

  async startRecording(sessionId?: string) {
    if (this.selectedDeviceIds.size === 0) {
      this.error = "Select at least one audio device";
      return;
    }
    this.error = null;
    this.pending = 'starting';
    try {
      const res = await api.startRecording(
        [...this.selectedDeviceIds],
        sessionId,
      );
      this.activeSessionId = res.session_id;
      this.isRecording = true;
      this.problems = {};
      this.recordingDuration = 0;
      this.durationInterval = setInterval(() => {
        this.recordingDuration++;
      }, 1000);
      return res;
    } catch (e) {
      this.error = e instanceof Error ? e.message : "Failed to start recording";
    } finally {
      this.pending = null;
    }
  }

  /** Show a recording that was already running when the page connected: the app was
   *  restarted (or crashed) while the backend kept recording. */
  resume(sessionId: string, startedAt: number, deviceIds: number[], problems: Record<string, string> = {}) {
    this.activeSessionId = sessionId;
    this.isRecording = true;
    this.selectedDeviceIds = new Set(deviceIds);
    this.problems = problems;
    this.recordingDuration = Math.max(0, Math.round(Date.now() / 1000 - startedAt));
    if (this.durationInterval) clearInterval(this.durationInterval);
    this.durationInterval = setInterval(() => {
      this.recordingDuration++;
    }, 1000);
  }

  /** A source failed: save what was recorded and carry on into the same meeting. The timer
   *  keeps running; the backend's devices may have new ids (a recreated echo canceller). */
  async restartCapture() {
    const sessionId = this.activeSessionId;
    if (!sessionId || this.pending) return;
    this.pending = 'starting';
    this.error = null;
    try {
      const res = await api.restartRecording(sessionId);
      this.problems = {};
      const active = (await api.getActiveRecordings()).find((a) => a.session_id === sessionId);
      if (active) this.selectedDeviceIds = new Set(active.device_ids);
      return res;
    } catch (e) {
      this.error = e instanceof Error ? e.message : 'Failed to restart the recording';
    } finally {
      this.pending = null;
    }
  }

  async stopRecording(transcribe?: boolean) {
    if (!this.activeSessionId) return;
    this.error = null;
    this.pending = 'stopping';
    try {
      const res = await api.stopRecording(this.activeSessionId, transcribe);
      this.isRecording = false;
      if (this.durationInterval) {
        clearInterval(this.durationInterval);
        this.durationInterval = null;
      }
      this.activeSessionId = null;
      this.levels = {};
      this.problems = {};
      return res;
    } catch (e) {
      this.error = e instanceof Error ? e.message : "Failed to stop recording";
    } finally {
      this.pending = null;
    }
  }
}

export const audioState = new AudioState();
