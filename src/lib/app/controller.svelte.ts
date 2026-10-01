/**
 * App behaviour that spans stores: connecting to the backend, recording actions
 * (buttons, shortcuts, tray, command line, calendar banner) and keyboard shortcuts.
 */
import {
  addBookmark,
  exportToObsidian,
  getActiveRecordings,
  getEncryption,
  getHealth,
  getJob,
  getMe,
  redeemInvite,
  getSettings,
  getSystemInfo,
  listSessions
} from '$lib/api/backend.js';
import { browserCapture } from '$lib/app/browser-capture.js';
import { askState } from '$lib/stores/ask.svelte.js';
import { digestState } from '$lib/stores/digest.svelte.js';
import { audioState } from '$lib/stores/audio.svelte.js';
import { autoRecordState, type AutoMode } from '$lib/stores/autorecord.svelte.js';
import { calendarState } from '$lib/stores/calendar.svelte.js';
import { connectionState, SAME_ORIGIN } from '$lib/stores/connection.svelte.js';
import { jobsState } from '$lib/stores/jobs.svelte.js';
import { sessionState } from '$lib/stores/session.svelte.js';
import { toastState } from '$lib/stores/toast.svelte.js';
import { teamState } from '$lib/stores/team.svelte.js';
import { transcriptState } from '$lib/stores/transcript.svelte.js';
import { uiState, type ShellStage, type View } from '$lib/stores/ui.svelte.js';
import { updateState } from '$lib/stores/update.svelte.js';
import { wsState } from '$lib/stores/websocket.svelte.js';
import type { BackendEvent, RecoveredRecording } from '$lib/types/index.js';

// ---- navigation ----------------------------------------------------------------

/** Show a view in the main area (closing any open meeting). */
export function openView(view: View) {
  uiState.view = view;
  if (view !== 'home') sessionState.show(null);
}

/** Header buttons: open a view, or go back home when it is already open. */
export function toggleView(view: View) {
  openView(uiState.view === view && !sessionState.activeSession ? 'home' : view);
}

export const openSetup = () => openView('setup');

// ---- recording -----------------------------------------------------------------

/** How the people in a recording agreed to it (backend routes/audio.py Consent). */
export type Consent = 'all_parties' | 'in_person' | 'one_party';

export async function startRecording(fresh = false, consent?: Consent) {
  if (audioState.pending) return; // a click already on its way
  if (connectionState.consentRequired && !consent) {
    uiState.consentAsk = { fresh }; // ConsentDialog asks, then comes back here
    return;
  }
  // In the browser the microphone and share dialogs come first, straight from the click.
  if (audioState.inBrowser && !(await audioState.acquireBrowser())) {
    toastState.error(audioState.error ?? 'Could not start recording');
    return;
  }
  audioState.pending = 'starting'; // at once, before the session exists
  try {
    if (fresh || !sessionState.activeSession) {
      await sessionState.createSession();
    }
    if (!sessionState.activeSession) return;
    const sessionId = sessionState.activeSession.id;
    const res = await audioState.startRecording(sessionId, consent);
    if (res) {
      transcriptState.startLive(sessionId);
      toastState.info(res.live_job_id ? 'Recording started, live transcript on' : 'Recording started');
      uiState.activeTab = 'recording';
    }
  } finally {
    audioState.pending = null;
  }
}

export async function stopAndTranscribe() {
  if (audioState.pending) return;
  const sessionId = audioState.activeSessionId;
  const result = await audioState.stopRecording();
  if (!result || !sessionId) return;
  sessionState.show(result.session);
  // The backend answers as soon as capture stops; encoding and mixing follow as a job.
  if (result.will_transcribe) {
    transcriptState.expectJob(sessionId, 'Saving the recording…');
    uiState.activeTab = 'transcript';
  } else {
    toastState.info('Recording stopped; saving it');
  }
}

/** The app was restarted while the backend kept recording (it waits a while for the app
 *  to come back; backend/mnemosyne/api/app_watch.py): show that recording, Stop and all. */
async function resumeActiveRecording() {
  if (audioState.isRecording || audioState.pending) return;
  const [active] = await getActiveRecordings().catch(() => []);
  if (!active || audioState.isRecording) return;
  audioState.resume(active.session_id, active.started_at, active.device_ids, active.problems);
  // The sources line names the devices: a fresh window has not loaded them yet.
  if (!audioState.devices.length) void audioState.loadDevices();
  transcriptState.resumeLive(
    active.session_id,
    active.live_segments.map((s) => s.segment),
    active.live
  );
  await sessionState.selectSession(active.session_id);
  uiState.view = 'home';
  uiState.activeTab = 'recording';
  toastState.info('Still recording: picked up the recording in progress');
}

/** After a reconnect: a recording shown here that the backend no longer has (it restarted,
 *  and recovers what was recorded) is shown as stopped, not as recording on forever. */
async function checkRecordingStillRunning() {
  const sessionId = audioState.activeSessionId;
  if (!audioState.isRecording || !sessionId || audioState.pending) return;
  const active = await getActiveRecordings().catch(() => null);
  if (!active || active.some((a) => a.session_id === sessionId)) return;
  if (audioState.activeSessionId !== sessionId || audioState.pending) return;
  audioState.ended();
  toastState.show('The recording stopped when the backend restarted. What was recorded is kept.', 'error', 20_000);
}

/** A source stopped being captured: save what was recorded and carry on recording into the
 *  same meeting (its next part). The live transcript so far stays. */
export async function restartCapture() {
  const elapsed = audioState.recordingDuration;
  const res = await audioState.restartCapture();
  if (!res) {
    if (audioState.error) toastState.error(audioState.error);
    return;
  }
  transcriptState.continueLive(elapsed);
  toastState.success('Recording again; what was recorded so far is saved');
}

function announceCaptureHealth(msg: Extract<BackendEvent, { type: 'capture_health' }>) {
  if (msg.session_id !== audioState.activeSessionId) return;
  if (msg.state === 'ok') {
    toastState.success(msg.message);
    return;
  }
  toastState.show(`${msg.message}. What was recorded is safe.`, 'error', 60_000, {
    label: 'Restart capture',
    run: restartCapture
  });
  notifyDesktop('Recording problem', `${msg.message}. Open Mnemosyne to restart the capture.`);
}

/** Mark this moment of the recording as important (Mark button, Ctrl+M, tray,
 *  `mnemosyne --mark`). The summary gives marked moments weight. */
export async function markMoment() {
  const sessionId = audioState.activeSessionId;
  if (!audioState.isRecording || !sessionId) {
    toastState.info('Marks are for a recording in progress');
    return;
  }
  try {
    const b = await addBookmark(sessionId);
    audioState.marks += 1;
    const m = Math.floor(b.at / 60);
    toastState.success(`Marked ${m}:${String(Math.floor(b.at % 60)).padStart(2, '0')}`);
  } catch (e) {
    toastState.error(e instanceof Error ? e.message : 'Could not mark this moment');
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

sessionState.onError = (message) => toastState.error(message);
transcriptState.isRecordingLive = () =>
  audioState.isRecording && audioState.activeSessionId === transcriptState.liveSessionId;

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

/** What went wrong when the backend started (a damaged settings file, a recovery that
 * failed): said once per backend, not on every reconnect. */
const shownProblems = new Set<string>();
async function showStartupProblems() {
  try {
    const { problems } = await getSystemInfo();
    for (const p of problems) {
      if (shownProblems.has(p)) continue;
      shownProblems.add(p);
      toastState.show(p, 'error', 20_000);
    }
  } catch {
    /* an older backend, or not reachable yet */
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
    autoRecordState.calendar = s.values.auto_record_calendar ?? true;
    autoRecordState.typeWords = (s.values.meeting_types ?? [])
      .filter((t) => t.auto_record)
      .map((t) => t.match.split(',').map((w) => w.trim().toLowerCase()).filter(Boolean));
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
    const byType = (title: string) =>
      autoRecordState.typeWords.some((words) => words.some((w) => title.toLowerCase().includes(w)));
    const wanted = ev && ((autoRecordState.mode === 'auto' && autoRecordState.calendar) || byType(ev.title));
    if (ev && wanted && !calendarHandled.has(ev.uid)) {
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

// ---- restore ---------------------------------------------------------------------

/** After a restore was staged: restart the backend (it restores on start) and reload the page
 *  so every store starts afresh. False outside the desktop app: restart the backend by hand. */
export async function restartBackendForRestore(): Promise<boolean> {
  let desktop = false;
  try {
    desktop = (await import('@tauri-apps/api/core')).isTauri();
  } catch {
    /* plain browser */
  }
  if (!desktop) return false;
  await invokeShell('restart_backend');
  await new Promise((r) => setTimeout(r, 1500)); // the old backend is gone by now
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    if (await getHealth().then(() => true).catch(() => false)) break;
    await new Promise((r) => setTimeout(r, 1000));
  }
  location.reload();
  return true;
}

// ---- quitting while recording ------------------------------------------------------

/** The quit dialog: stop, wait until the recording is saved (not transcribed: quitting would
 *  cut that off), then quit. */
export async function stopSaveAndQuit() {
  uiState.quitSaving = true;
  try {
    const res = await audioState.stopRecording(false);
    if (!res) {
      toastState.error(audioState.error ?? 'Could not stop the recording');
      return;
    }
    if (res.job_id) await waitForJob(res.job_id);
    await invokeShell('quit_app');
  } finally {
    uiState.quitSaving = false;
    uiState.quitAsk = false;
  }
}

/** The quit dialog: hide the window; the recording goes on (tray, or launch again, to return). */
export async function keepRecordingInBackground() {
  uiState.quitAsk = false;
  await invokeShell('hide_window');
  notifyDesktop('Still recording', 'Mnemosyne records on in the background. Open it again, or use the tray, to stop.');
}

async function waitForJob(jobId: string) {
  for (;;) {
    const job = await getJob(jobId).catch(() => null);
    if (!job || job.status === 'completed' || job.status === 'failed' || job.status === 'cancelled') return job;
    await new Promise((r) => setTimeout(r, 500));
  }
}

/** start-record | stop-record | toggle-record, from the tray, CLI, or calendar banner;
 *  quit-requested when the window is closed or Quit chosen during a recording. */
export async function handleRemoteAction(action: string) {
  if (action === 'mark') {
    await markMoment();
    return;
  }
  if (action === 'quit-requested') {
    if (audioState.isRecording) uiState.quitAsk = true;
    else await invokeShell('quit_app'); // it stopped meanwhile
    return;
  }
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
  // The palette opens from anywhere, text fields included.
  if (e.ctrlKey && e.key === 'k') {
    e.preventDefault();
    uiState.paletteOpen = !uiState.paletteOpen;
    return;
  }
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
    m: () => void markMoment(),
    b: () => (uiState.sidebarCollapsed = !uiState.sidebarCollapsed)
  };
  const action = actions[e.key];
  if (action) {
    e.preventDefault();
    action();
  }
}

// ---- recovered recordings ---------------------------------------------------------

const announcedRecoveries = new Set<string>();

/** Tell the user once per page that a recording cut short by a crash was saved. */
function announceRecovered(r: RecoveredRecording) {
  if (announcedRecoveries.has(r.session_id)) return;
  announcedRecoveries.add(r.session_id);
  sessionState.loadSessions();
  const length = r.seconds >= 60 ? ` (${Math.round(r.seconds / 60)} min)` : '';
  const message = `Recovered "${r.name}"${length} after an interrupted recording`;
  if (r.transcribing) {
    toastState.show(`${message}; transcribing it now`, 'success', 10_000);
    return;
  }
  toastState.show(message, 'success', 20_000, {
    label: 'Transcribe',
    run: async () => {
      await sessionState.selectSession(r.session_id);
      await transcriptState.transcribe(r.session_id);
    }
  });
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
  // The sidebar mounts before the backend is up in the desktop app (it is still starting or
  // installing), so the meeting list is loaded here, once there is a backend to ask.
  sessionState.loadSessions();
  wsState.connect();
  transcriptState.init();
  void resumeActiveRecording();
  jobsState.init();
  askState.init();
  digestState.init();
  const stopAutoRecord = listenForAutoRecord();
  maybeRunSetup();
  void showStartupProblems();
  void import('./smoke.js').then((m) => m.runSmokeTest());
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
    if (msg.type === 'capture_health') {
      announceCaptureHealth(msg);
      return;
    }
    if (msg.type === 'bookmarks' || msg.type === 'assets') {
      if (sessionState.activeSession?.id === msg.session_id) sessionState.refreshActive();
      return;
    }
    if (msg.type === 'hello') {
      // Every (re)connect, e.g. after the backend restarted to pick up GPU support. What
      // changed while we were away is fetched again.
      sessionState.loadSessions();
      sessionState.refreshActive();
      for (const r of msg.recovered ?? []) announceRecovered(r);
      void checkRecordingStillRunning();
      void resumeActiveRecording();
      return;
    }
    if (msg.type === 'recovered') {
      announceRecovered(msg);
      return;
    }
    if (msg.type === 'shares') {
      // Some meeting was shared or unshared: what this person may see may have changed.
      sessionState.loadSessions();
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
/** A connection through remote access needs this app's tunnel up, and its current port (the
 * preferred one may have been taken since the last launch). */
async function syncRemoteTunnel() {
  if (!connectionState.remote) return;
  try {
    const { invoke, isTauri } = await import('@tauri-apps/api/core');
    if (!isTauri()) return;
    const url = await invoke<string | null>('remote_start');
    if (url && url !== connectionState.url) connectionState.save(url, connectionState.token, true);
  } catch (e) {
    console.warn('Remote access tunnel:', e);
  }
}

/** "Chrome on Windows": how an admin tells this browser apart in the list of people. */
function deviceName(): string {
  const ua = navigator.userAgent;
  const browser = /Edg\//.test(ua) ? 'Edge' : /Chrome\//.test(ua) ? 'Chrome' : /Firefox\//.test(ua) ? 'Firefox' : /Safari\//.test(ua) ? 'Safari' : 'Browser';
  const os = /Windows/.test(ua) ? 'Windows' : /Mac OS X/.test(ua) ? 'Mac' : /Android/.test(ua) ? 'Android' : /iPhone|iPad/.test(ua) ? 'iOS' : /Linux/.test(ua) ? 'Linux' : '';
  return os ? `${browser} on ${os}` : browser;
}

/** An invite link (`?invite=<code>`, a team server) becomes this browser's own token. */
async function redeemInviteFromUrl() {
  const params = new URLSearchParams(location.search);
  const code = params.get('invite');
  if (!code) return;
  params.delete('invite');
  const rest = params.toString();
  history.replaceState(null, '', location.pathname + (rest ? `?${rest}` : '') + location.hash);
  try {
    const { token } = await redeemInvite(code, deviceName());
    connectionState.save(connectionState.url, token);
    uiState.signInError = '';
  } catch (e) {
    uiState.signInError = e instanceof Error ? e.message.replace(/^\d+: /, '') : 'This invite link did not work';
  }
}

/** Who is signed in; false when the server wants someone and nobody (valid) is. */
async function signedIn(): Promise<boolean> {
  if (!connectionState.authRequired) {
    await refreshMe(); // the desktop app: an admin, and whether supervision is on
    return true;
  }
  try {
    connectionState.me = await getMe();
    if (connectionState.me.team_mode) void teamState.load();
    return true;
  } catch (e) {
    if (e instanceof Error && e.message.startsWith('401')) return false;
    throw e;
  }
}

/** Reload who this is and what the server has on for them (after settings change). */
export async function refreshMe(): Promise<void> {
  try {
    connectionState.me = await getMe();
  } catch {
    // An older backend without /api/users/me: nothing role-dependent to show.
  }
}

export function connectApp(): () => void {
  // Recording in this browser: say when sharing stops or the recording ends elsewhere.
  browserCapture.onProblem = (message) => toastState.show(message, 'error', 20_000);
  let unsubscribeSessions: (() => void) | null = null;
  let unlistenShell: (() => void) | null = null;
  let cancelled = false;
  let attempts = 0;

  async function connect() {
    await syncRemoteTunnel();
    while (!cancelled) {
      try {
        const h = await getHealth();
        connectionState.host = h.host ?? null;
        connectionState.authRequired = !!h.auth_required;
        connectionState.consentRequired = !!h.consent_required;
        if (SAME_ORIGIN) await redeemInviteFromUrl();
        if (!(await signedIn())) {
          uiState.backendStatus = 'connected';
          uiState.signIn = true;
          return;
        }
        const encryption = await getEncryption().catch(() => null);
        if (encryption?.locked) {
          uiState.backendStatus = 'connected';
          uiState.locked = true; // the unlock screen reloads the page once the code is in
          return;
        }
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
