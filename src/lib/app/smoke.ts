/**
 * Release smoke test, run by CI against the built AppImage with MNEMOSYNE_SMOKE=1
 * (scripts/smoke-appimage.sh): open a meeting that has audio, play it muted and report whether
 * the page survived. The shell prints the result and exits; a crashed WebKit web process fails
 * the test there (lib.rs). 0.7.1 shipped an AppImage whose web process died on the first
 * <audio> element, leaving a white window.
 */
import { listSessions } from '$lib/api/backend.js';
import { playerState } from '$lib/stores/player.svelte.js';
import { sessionState } from '$lib/stores/session.svelte.js';
import { uiState } from '$lib/stores/ui.svelte.js';
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
    uiState.activeTab = 'transcript'; // the player lives in the transcript view
    if (!(await waitFor(() => playerState.available, 10))) return report(false, 'no audio player');
    playerState.muted = true;
    playerState.toggle();
    if (!(await waitFor(() => playerState.duration > 0 || !playerState.available, 20))) {
      return report(false, 'audio metadata never loaded');
    }
    if (!playerState.available) return report(false, 'the audio element reported an error');
    // A broken media stack takes the web process down about now; the shell catches that.
    if (!(await waitFor(() => playerState.currentTime > 1, 15))) {
      return report(false, `playback did not advance (at ${playerState.currentTime.toFixed(2)} s)`);
    }
    await report(
      true,
      `played ${playerState.currentTime.toFixed(1)} of ${playerState.duration.toFixed(1)} s`
    );
  } catch (e) {
    await report(false, e instanceof Error ? e.message : String(e));
  }
}
