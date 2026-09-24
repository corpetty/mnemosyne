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
  activeSessionId = $state<string | null>(null);
  recordingDuration = $state(0);
  error = $state<string | null>(null);
  loading = $state(false);
  /** Live input levels per device id while recording (from `levels` events). */
  levels = $state<Record<string, Level>>({});
  private unsubscribeLevels: (() => void) | null = null;

  listenForLevels() {
    this.unsubscribeLevels?.();
    this.unsubscribeLevels = wsState.onMessage((raw) => {
      const msg = raw as BackendEvent;
      if (msg.type === 'levels' && msg.session_id === this.activeSessionId) this.levels = msg.levels;
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
    try {
      const res = await api.startRecording(
        [...this.selectedDeviceIds],
        sessionId,
      );
      this.activeSessionId = res.session_id;
      this.isRecording = true;
      this.recordingDuration = 0;
      this.durationInterval = setInterval(() => {
        this.recordingDuration++;
      }, 1000);
      return res;
    } catch (e) {
      this.error = e instanceof Error ? e.message : "Failed to start recording";
    }
  }

  async stopRecording() {
    if (!this.activeSessionId) return;
    this.error = null;
    try {
      const res = await api.stopRecording(this.activeSessionId);
      this.isRecording = false;
      if (this.durationInterval) {
        clearInterval(this.durationInterval);
        this.durationInterval = null;
      }
      this.activeSessionId = null;
      this.levels = {};
      return res;
    } catch (e) {
      this.error = e instanceof Error ? e.message : "Failed to stop recording";
    }
  }
}

export const audioState = new AudioState();
