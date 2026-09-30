import type { AudioDevice, BackendEvent, Level } from "$lib/types/index.js";
import * as api from "$lib/api/backend.js";
import { wsState } from "./websocket.svelte.js";
import { SAME_ORIGIN } from "./connection.svelte.js";
import { browserCapture, type Acquired } from "$lib/app/browser-capture.js";

// In a browser loaded from a server (a firm's), the sources are this browser's: its microphone
// and, when shared, the call's audio. They stand in the device list with these ids, which the
// backend uses for them too, so the source line and the level meters work as for PipeWire.
export const BROWSER_MIC = -1;
export const BROWSER_CALL = -2;
const BROWSER_DEVICES: AudioDevice[] = [
  { id: BROWSER_MIC, name: 'browser-mic', description: 'Microphone', media_class: 'Audio/Source', is_input: true, is_output: false, is_monitor: false, is_echo_cancelled: false },
  { id: BROWSER_CALL, name: 'browser-call', description: 'Call audio (shared from this browser)', media_class: 'Audio/Sink', is_input: false, is_output: true, is_monitor: false, is_echo_cancelled: false }
];
const MIC_KEY = 'mnemosyne.browserMic';

function recordInBrowserFlag(): boolean {
  try {
    return typeof localStorage !== 'undefined' && localStorage.getItem('mnemosyne.recordInBrowser') === '1';
  } catch {
    return false;
  }
}

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
  /** Recording happens in this browser (app/browser-capture.ts), not on the backend's machine:
   * the app was loaded from a server, or `mnemosyne.recordInBrowser` is "1" in localStorage
   * (development and the browser tests, with the UI served by Vite). */
  readonly inBrowser = SAME_ORIGIN || recordInBrowserFlag();
  /** The browser microphone chosen (a MediaDeviceInfo id), or null for the default one. */
  browserMicId = $state<string | null>(null);
  private acquired: Acquired | null = null;
  devices = $state<AudioDevice[]>([]);
  selectedDeviceIds = $state<Set<number>>(new Set());
  isRecording = $state(false);
  /** A click on Record / Stop waiting for the backend: shown at once, so the click registers. */
  pending = $state<'starting' | 'stopping' | null>(null);
  activeSessionId = $state<string | null>(null);
  recordingDuration = $state(0);
  error = $state<string | null>(null);
  loading = $state(false);
  /** Moments marked in this recording (markMoment). */
  marks = $state(0);
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

  /** The clock counts from the start time, not by ticks: a hidden window's timers are
   *  throttled, and a tick count falls behind the real length of the recording. */
  private startClock(startedAtMs: number) {
    this.stopClock();
    const tick = () => (this.recordingDuration = Math.max(0, Math.round((Date.now() - startedAtMs) / 1000)));
    tick();
    this.durationInterval = setInterval(tick, 1000);
  }

  private stopClock() {
    if (this.durationInterval) clearInterval(this.durationInterval);
    this.durationInterval = null;
  }

  /** The recording ended without a Stop from here (the backend restarted or stopped it). */
  ended() {
    this.isRecording = false;
    this.stopClock();
    this.activeSessionId = null;
    this.levels = {};
    this.problems = {};
  }

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
      this.devices = this.inBrowser ? BROWSER_DEVICES : await api.getDevices();
      this.restoreSelection();
      if (this.inBrowser && this.selectedDeviceIds.size === 0) {
        this.selectedDeviceIds = new Set([BROWSER_MIC, BROWSER_CALL]);
      }
      if (this.inBrowser) {
        try {
          this.browserMicId = localStorage.getItem(MIC_KEY);
        } catch {
          /* no storage */
        }
      }
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

  setBrowserMic(id: string | null) {
    this.browserMicId = id;
    try {
      if (id) localStorage.setItem(MIC_KEY, id);
      else localStorage.removeItem(MIC_KEY);
    } catch {
      /* no storage */
    }
  }

  /** In the browser: ask for the microphone and the call's audio. Run straight from the click
   * (before any request), since browsers only show the share dialog then. False on refusal. */
  async acquireBrowser(): Promise<boolean> {
    this.error = null;
    try {
      this.acquired = await browserCapture.acquire({
        mic: this.selectedDeviceIds.has(BROWSER_MIC),
        micId: this.browserMicId,
        call: this.selectedDeviceIds.has(BROWSER_CALL)
      });
      return true;
    } catch (e) {
      this.error = e instanceof Error ? e.message : 'Could not start recording in this browser';
      return false;
    }
  }

  /** Make sure devices are loaded and a remembered selection is applied. */
  async ensureDevices() {
    if (this.devices.length === 0) await this.loadDevices();
    return this.selectedDeviceIds.size > 0;
  }

  async startRecording(sessionId?: string, consent?: string | null) {
    if (this.selectedDeviceIds.size === 0) {
      this.error = "Select at least one audio device";
      return;
    }
    this.error = null;
    this.pending = 'starting';
    try {
      let res;
      if (this.inBrowser) {
        const got = this.acquired;
        if (!got) throw new Error('Recording was not allowed in this browser');
        this.acquired = null;
        try {
          res = await api.startBrowserRecording({
            sources: got.sources,
            sample_rate: got.sampleRate,
            labels: got.labels,
            session_id: sessionId ?? null,
            consent: consent ?? null
          });
        } catch (e) {
          await browserCapture.stop();
          throw e;
        }
        browserCapture.start(res.recording_id);
        this.selectedDeviceIds = new Set(got.sources.map((s) => (s === 'mic' ? BROWSER_MIC : BROWSER_CALL)));
        // "Recording from" names the microphone the browser actually opened.
        this.devices = BROWSER_DEVICES.map((d) =>
          d.id === BROWSER_MIC && got.labels.mic ? { ...d, description: got.labels.mic } : d
        );
      } else {
        res = await api.startRecording([...this.selectedDeviceIds], sessionId, consent);
      }
      this.activeSessionId = res.session_id;
      this.isRecording = true;
      this.problems = {};
      this.marks = 0;
      this.startClock(Date.now());
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
    this.startClock(startedAt * 1000);
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
      // The browser sends what it still holds before the backend stops listening.
      if (browserCapture.active) await browserCapture.stop();
      const res = await api.stopRecording(this.activeSessionId, transcribe);
      this.ended();
      return res;
    } catch (e) {
      this.error = e instanceof Error ? e.message : "Failed to stop recording";
    } finally {
      this.pending = null;
    }
  }
}

export const audioState = new AudioState();
