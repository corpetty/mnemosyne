import type { AudioDevice } from "$lib/types/index.js";
import * as api from "$lib/api/backend.js";

class AudioState {
  devices = $state<AudioDevice[]>([]);
  selectedDeviceIds = $state<Set<number>>(new Set());
  isRecording = $state(false);
  activeSessionId = $state<string | null>(null);
  recordingDuration = $state(0);
  error = $state<string | null>(null);
  loading = $state(false);

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
      const sessionId = this.activeSessionId;
      this.activeSessionId = null;
      return { ...res, session_id: sessionId };
    } catch (e) {
      this.error = e instanceof Error ? e.message : "Failed to stop recording";
    }
  }
}

export const audioState = new AudioState();
