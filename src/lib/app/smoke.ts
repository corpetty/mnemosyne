/**
 * Release smoke test, run by CI against the built AppImage with MNEMOSYNE_SMOKE=1
 * (scripts/smoke-appimage.sh): open a meeting that has audio, play its audio muted and report
 * whether the page survived. The shell prints the result and exits; a crashed WebKit web process
 * fails the test there (lib.rs). 0.7.1 shipped an AppImage whose web process died on the first
 * <audio> element, leaving a white window.
 */
import { audioUrl, listSessions } from '$lib/api/backend.js';
import { sessionState } from '$lib/stores/session.svelte.js';
import { invokeShell } from './controller.svelte.js';

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function waitFor(check: () => boolean, seconds: number): Promise<boolean> {
  for (let i = 0; i < seconds * 4; i++) {
    if (check()) return true;
    await sleep(250);
  }
  return check();
}

export async function runSmokeTest() {
  if (!(await invokeShell<boolean>('is_smoke_test'))) return;
  const report = (ok: boolean, detail: string) => invokeShell('smoke_result', { ok, detail });
  try {
    // The first launch installs the backend; CI imports a meeting once it is up.
    let sessionId: string | null = null;
    for (let i = 0; i < 1200 && !sessionId; i++) {
      try {
        sessionId = (await listSessions()).find((s) => s.has_audio)?.id ?? null;
      } catch {
        // backend not up yet
      }
      if (!sessionId) await sleep(1000);
    }
    if (!sessionId) return report(false, 'no meeting with audio appeared');
    await sessionState.selectSession(sessionId);

    // The same media path as the meeting's player (which needs a transcript to show).
    const audio = new Audio(audioUrl(sessionId));
    audio.muted = true;
    let failure = '';
    audio.onerror = () => (failure = `media error ${audio.error?.code}: ${audio.error?.message}`);
    document.body.appendChild(audio);
    await audio.play().catch((e) => (failure ||= `play() failed: ${e}`));
    // A broken media stack takes the web process down about now; the shell catches that.
    await waitFor(() => audio.currentTime > 2 || failure !== '', 20);
    if (failure) return report(false, failure);
    if (audio.currentTime <= 2) {
      return report(false, `playback did not advance (at ${audio.currentTime.toFixed(2)} s)`);
    }
    await report(true, `played ${audio.currentTime.toFixed(1)} of ${audio.duration.toFixed(1)} s`);
  } catch (e) {
    await report(false, e instanceof Error ? e.message : String(e));
  }
}
