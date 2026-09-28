import { getDiagnostics } from '$lib/api/backend.js';

export const ISSUES_URL = 'https://github.com/corpetty/mnemosyne/issues/new';

/** The backend's diagnostics report, headed by the app version (or the browser). */
export async function collectDiagnostics(): Promise<{ text: string; logFile: string }> {
  const d = await getDiagnostics();
  let head = '';
  try {
    const { getVersion } = await import('@tauri-apps/api/app');
    head = `app ${await getVersion()}\n`;
  } catch {
    head = `browser ${navigator.userAgent}\n`;
  }
  return { text: head + d.text, logFile: d.log_file };
}

/** Open a link in the user's browser (the desktop app cannot navigate its own window away). */
export async function openExternal(href: string) {
  try {
    const { invoke } = await import('@tauri-apps/api/core');
    await invoke('plugin:shell|open', { path: href });
  } catch {
    window.open(href, '_blank', 'noopener,noreferrer');
  }
}
