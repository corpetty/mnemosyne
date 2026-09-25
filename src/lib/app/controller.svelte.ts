/**
 * App behaviour that spans stores: connecting to the backend, recording actions
 * (buttons, shortcuts, tray, command line, calendar banner) and keyboard shortcuts.
 */
import { exportToObsidian, getHealth, getSettings, listSessions } from '$lib/api/backend.js';
import { askState } from '$lib/stores/ask.svelte.js';
import { digestState } from '$lib/stores/digest.svelte.js';
import { audioState } from '$lib/stores/audio.svelte.js';
import { autoRecordState, type AutoMode } from '$lib/stores/autorecord.svelte.js';
import { calendarState } from '$lib/stores/calendar.svelte.js';
import { connectionState } from '$lib/stores/connection.svelte.js';
import { jobsState } from '$lib/stores/jobs.svelte.js';
import { sessionState } from '$lib/stores/session.svelte.js';
import { toastState } from '$lib/stores/toast.svelte.js';
import { transcriptState } from '$lib/stores/transcript.svelte.js';
import { uiState, type ShellStage, type View } from '$lib/stores/ui.svelte.js';
import { updateState } from '$lib/stores/update.svelte.js';
import { wsState } from '$lib/stores/websocket.svelte.js';
import type { BackendEvent } from '$lib/types/index.js';

// ---- navigation ----------------------------------------------------------------

/** Show a view in the main area (closing any open meeting). */
export function openView(view: View) {
  uiState.view = view;
  if (view !== 'home') sessionState.activeSession = null;
}

/** Header buttons: open a view, or go back home when it is already open. */
export function toggleView(view: View) {
  openView(uiState.view === view && !sessionState.activeSession ? 'home' : view);
}

export const openAsk = () => openView('ask');
export const openSetup = () => openView('setup');

// ---- recording -----------------------------------------------------------------

export async function startRecording(fresh = false) {
  if (fresh || !sessionState.activeSession) {
    await sessionState.createSession();
  }
  if (!sessionState.activeSession) return;
  const sessionId = sessionState.activeSession.id;
  const res = await audioState.startRecording(sessionId);
  if (res) {
    transcriptState.startLive(sessionId);
    toastState.info(res.live_job_id ? 'Recording started, live transcript on' : 'Recording started');
    uiState.activeTab = 'recording';
  }
}

export async function stopAndTranscribe() {
  const sessionId = audioState.activeSessionId;
  const result = await audioState.stopRecording();
  if (!result || !sessionId) return;
  sessionState.activeSession = result.session;
  if (result.job_id) {
    toastState.info('Transcription queued');
    transcriptState.expectJob(sessionId);
    uiState.activeTab = 'transcript';
  } else {
    toastState.info('Recording saved');
  }
}

export async function exportActive() {
  if (!sessionState.activeSession) return;
  try {
    const result = await exportToObsidian(sessionState.activeSession.id);
    toastState.success(`Exported to ${result.path}`);
  } catch (e) {
    toastState.error(e instanceof Error ? e.message : 'Export failed');
  }
}

// ---- first run -------------------------------------------------------------------

/** Open the setup wizard on a fresh install: setup never completed and no meetings yet. */
async function maybeRunSetup() {
  try {
    const [settings, sessions] = await Promise.all([getSettings(), listSessions()]);
    if (!settings.values.setup_complete && sessions.length === 0 && uiState.view === 'home') {
      openView('setup');
    }
  } catch {
    /* not reachable yet; the next connect tries again */
  }
}

// ---- auto-record ---------------------------------------------------------------------

const APP_GRACE_MS = 60_000; // the meeting app may drop its stream briefly (device switch)
const CALENDAR_GRACE_MS = 5 * 60_000;
let lastSound = Date.now();
let appGoneAt: number | null = null;
const offeredApps = new Set<string>(); // ask once per app appearance
const calendarHandled = new Set<string>();

export async function loadAutoRecordSettings() {
  try {
    const s = await getSettings();
    autoRecordState.mode = (s.values.auto_record as AutoMode) || 'off';
    autoRecordState.silenceMinutes = s.values.auto_stop_silence_minutes ?? 10;
  } catch {
    /* keep previous */
  }
}

async function autoStart(started: { by: 'app' | 'calendar'; app?: string; uid?: string; end?: string }, why: string) {
  if (audioState.isRecording) return;
  await startRecording(true);
  if (audioState.isRecording) {
    autoRecordState.started = started;
    lastSound = Date.now();
    appGoneAt = null;
    toastState.info(`Recording started: ${why}`);
    notifyDesktop('Mnemosyne is recording', why);
  }
}

async function autoStop(reason: string) {
  autoRecordState.started = null;
  appGoneAt = null;
  if (!audioState.isRecording) return;
  await stopAndTranscribe();
  toastState.info(`Recording stopped: ${reason}`);
}

function onMeetingApp(status: 'started' | 'stopped', app: string) {
  const mode = autoRecordState.mode;
  const started = autoRecordState.started;
  if (status === 'stopped') {
    offeredApps.delete(app);
    if (autoRecordState.offer === app) autoRecordState.offer = null;
    if (started?.by === 'app' && started.app === app) appGoneAt = Date.now();
    return;
  }
  if (started?.by === 'app' && started.app === app) appGoneAt = null; // came back
  if (mode === 'off' || audioState.isRecording) return;
  if (mode === 'auto') {
    autoStart({ by: 'app', app }, `${app} is using the microphone`);
  } else if (!offeredApps.has(app)) {
    offeredApps.add(app);
    autoRecordState.offer = app;
    notifyDesktop(`${app} is using the microphone`, 'Record this meeting in Mnemosyne?');
  }
}

export function acceptAutoRecordOffer() {
  const app = autoRecordState.offer;
  autoRecordState.offer = null;
  if (app) autoStart({ by: 'app', app }, `${app} is using the microphone`);
}

function autoRecordTick() {
  const now = Date.now();
  const started = autoRecordState.started;
  if (!audioState.isRecording) {
    autoRecordState.started = null;
    const ev = calendarState.starting;
    if (autoRecordState.mode === 'auto' && ev && !calendarHandled.has(ev.uid)) {
      calendarHandled.add(ev.uid);
      autoStart({ by: 'calendar', uid: ev.uid, end: ev.end }, `${ev.title} is starting`);
    }
    return;
  }
  if (!started) return; // started by hand: never stopped automatically
  if (started.by === 'app' && appGoneAt !== null && now - appGoneAt > APP_GRACE_MS) {
    autoStop(`${started.app} stopped using the microphone`);
  } else if (started.by === 'calendar' && started.end && now > new Date(started.end).getTime() + CALENDAR_GRACE_MS) {
    autoStop('the meeting ended');
  } else if (autoRecordState.silenceMinutes > 0 && now - lastSound > autoRecordState.silenceMinutes * 60_000) {
    autoStop(`${autoRecordState.silenceMinutes} minutes of silence`);
  }
}

function listenForAutoRecord(): () => void {
  loadAutoRecordSettings();
  const timer = setInterval(autoRecordTick, 5000);
  const off = wsState.onMessage((raw) => {
    const msg = raw as BackendEvent;
    if (msg.type === 'meeting_app') onMeetingApp(msg.status, msg.app);
    else if (msg.type === 'levels' && Object.values(msg.levels).some((l) => l.rms_db > -50)) lastSound = Date.now();
  });
  return () => {
    clearInterval(timer);
    off();
  };
}

// ---- Tauri shell (tray, command line) ---------------------------------------------

export async function invokeShell<T>(cmd: string, args?: Record<string, unknown>): Promise<T | null> {
  try {
    const { invoke } = await import('@tauri-apps/api/core');
    return await invoke<T>(cmd, args);
  } catch {
    return null; // plain browser, or older shell
  }
}

/** start-record | stop-record | toggle-record, from the tray, CLI, or calendar banner. */
export async function handleRemoteAction(action: string) {
  const wantStart = action === 'start-record' || (action === 'toggle-record' && !audioState.isRecording);
  const wantStop = action === 'stop-record' || (action === 'toggle-record' && audioState.isRecording);
  if (wantStop && audioState.isRecording) {
    await stopAndTranscribe();
  } else if (wantStart && !audioState.isRecording) {
    if (uiState.backendStatus !== 'connected') return;
    if (!(await audioState.ensureDevices())) {
      await invokeShell('show_window');
      uiState.closePanels();
      await sessionState.createSession();
      uiState.activeTab = 'recording';
      toastState.error('Choose a microphone and/or system audio first, then start again');
      return;
    }
    await startRecording(true);
  }
}

/** Listen for tray / second-launch actions. Returns a cleanup function. */
export function listenForRemoteActions(): () => void {
  let unlisten: (() => void) | null = null;
  let cancelled = false;
  import('@tauri-apps/api/event')
    .then(({ listen }) => listen<string>('tray-action', (e) => handleRemoteAction(e.payload)))
    .then((un) => {
      if (cancelled) un();
      else unlisten = un;
    })
    .catch(() => {});
  return () => {
    cancelled = true;
    unlisten?.();
  };
}

let updateChecked = false;
/** Look for an app update once per run, shortly after the backend is up. */
export function checkForUpdateOnce() {
  if (uiState.backendStatus !== 'connected' || updateChecked) return;
  updateChecked = true;
  updateState.probe().then((ok) => {
    if (ok) setTimeout(() => updateState.check(), 8000);
  });
}

let launchActionChecked = false;
/** Run an action given on the command line of the first launch, once connected. */
export function runLaunchActionOnce() {
  if (uiState.backendStatus !== 'connected' || launchActionChecked) return;
  launchActionChecked = true;
  invokeShell<string | null>('take_launch_action').then((a) => {
    if (a) handleRemoteAction(a);
  });
}

// ---- keyboard ------------------------------------------------------------------

export function handleKeydown(e: KeyboardEvent) {
  const tag = (e.target as HTMLElement)?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
  if (!e.ctrlKey) return;
  const actions: Record<string, () => void> = {
    r: () => {
      if (!audioState.isRecording) startRecording();
    },
    s: () => {
      if (audioState.isRecording) stopAndTranscribe();
    },
    e: () => exportActive(),
    k: () => openAsk(),
    b: () => (uiState.sidebarCollapsed = !uiState.sidebarCollapsed)
  };
  const action = actions[e.key];
  if (action) {
    e.preventDefault();
    action();
  }
}

// ---- mention alerts --------------------------------------------------------------

function announceMention(keyword: string, speaker: string, text: string) {
  toastState.show(`“${keyword}” came up · ${speaker}: ${text}`, 'info', 10_000);
  notifyDesktop(`${keyword} was mentioned`, `${speaker}: ${text}`);
}

/** Desktop notification: the Tauri plugin in the app, the Notification API in a browser. */
export async function notifyDesktop(title: string, body: string) {
  try {
    const n = await import('@tauri-apps/plugin-notification');
    let granted = await n.isPermissionGranted();
    if (!granted) granted = (await n.requestPermission()) === 'granted';
    if (granted) n.sendNotification({ title, body });
    return;
  } catch {
    /* not in the desktop shell */
  }
  try {
    if (typeof Notification === 'undefined') return;
    if (Notification.permission === 'default') await Notification.requestPermission();
    if (Notification.permission === 'granted') new Notification(title, { body });
  } catch {
    /* notifications unavailable */
  }
}

// ---- backend connection ------------------------------------------------------------

function onConnected(): () => void {
  uiState.backendStatus = 'connected';
  wsState.connect();
  transcriptState.init();
  jobsState.init();
  askState.init();
  digestState.init();
  const stopAutoRecord = listenForAutoRecord();
  maybeRunSetup();
  calendarState.start();
  audioState.listenForLevels();
  jobsState.onComplete((job) => {
    if (job.kind !== 'summarize' || !job.session_id) return;
    if (sessionState.activeSession?.id === job.session_id) sessionState.refreshActive();
    sessionState.loadSessions();
    toastState.success('Summary ready');
  });
  transcriptState.onComplete((sessionId) => {
    if (sessionState.activeSession?.id === sessionId) sessionState.refreshActive();
    sessionState.loadSessions();
    toastState.success('Transcription complete');
  });
  // Keep the sidebar in step with backend session state changes.
  const stopSessions = wsState.onMessage((raw) => {
    const msg = raw as BackendEvent;
    if (msg.type === 'mention') {
      announceMention(msg.keyword, msg.speaker, msg.text);
      return;
    }
    if (msg.type !== 'session') return;
    sessionState.loadSessions();
    // Keep the open session's status (footer, badges) in step without a refetch.
    const active = sessionState.activeSession;
    if (active && active.id === msg.session_id && msg.status !== 'deleted' && msg.status !== 'audio_deleted') {
      active.status = msg.status;
    }
    if (msg.status === 'audio_deleted' && sessionState.activeSession?.id === msg.session_id) {
      sessionState.refreshActive();
    }
  });
  return () => {
    stopSessions();
    stopAutoRecord();
  };
}

function restartBackendWhenIdle() {
  const busy = () =>
    audioState.isRecording ||
    Object.values(jobsState.jobs).some((j) => (j.status === 'queued' || j.status === 'running') && j.kind !== 'live');
  const attempt = () => {
    if (busy()) {
      setTimeout(attempt, 10_000);
      return;
    }
    uiState.gpuInstall = { state: 'done', message: 'GPU support installed; restarting the backend…' };
    invokeShell('restart_backend');
    setTimeout(() => (uiState.gpuInstall = null), 30_000);
  };
  attempt();
}

/**
 * Poll until the backend answers, then start the stores. In release builds the first
 * launch installs dependencies and can take minutes; the shell reports progress.
 * Returns a cleanup function.
 */
export function connectApp(): () => void {
  let unsubscribeSessions: (() => void) | null = null;
  let unlistenShell: (() => void) | null = null;
  let cancelled = false;
  let attempts = 0;

  async function connect() {
    while (!cancelled) {
      try {
        const h = await getHealth();
        connectionState.host = h.host ?? null;
        connectionState.authRequired = !!h.auth_required;
        if (!cancelled) unsubscribeSessions = onConnected();
        return;
      } catch {
        attempts++;
        const stage = uiState.shellStage;
        if (attempts >= 3 && stage !== 'installing' && stage !== 'starting') {
          uiState.backendStatus = 'unreachable';
        }
        await new Promise((r) => setTimeout(r, 2000));
      }
    }
  }

  // GPU support installs in the background after the first start; restart the backend
  // to pick it up once nothing is recording or running.
  import('@tauri-apps/api/event')
    .then(({ listen }) =>
      listen<{ state: 'installing' | 'done' | 'error'; message: string; restart: boolean }>('gpu-install', (e) => {
        uiState.gpuInstall = { state: e.payload.state, message: e.payload.message };
        if (e.payload.state === 'done' && e.payload.restart) restartBackendWhenIdle();
      })
    )
    .catch(() => {});

  // Shell events only exist inside Tauri; ignore when running in a plain browser.
  import('@tauri-apps/api/event')
    .then(({ listen }) =>
      listen<{ stage: ShellStage; message: string }>('backend-status', (e) => {
        uiState.shellStage = e.payload.stage;
        uiState.shellMessage = e.payload.message;
        if (e.payload.stage === 'installing') {
          uiState.shellLog = [...uiState.shellLog.slice(-7), e.payload.message];
        }
        if (e.payload.stage === 'error') uiState.backendStatus = 'unreachable';
      })
    )
    .then((un) => {
      if (cancelled) un();
      else unlistenShell = un;
    })
    .catch(() => {});

  connect();

  return () => {
    cancelled = true;
    unlistenShell?.();
    unsubscribeSessions?.();
    transcriptState.destroy();
    jobsState.destroy();
    askState.destroy();
    digestState.destroy();
    calendarState.stop();
    wsState.disconnect();
  };
}
