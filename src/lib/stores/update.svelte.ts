import type { Update } from '@tauri-apps/plugin-updater';

type Status = 'idle' | 'checking' | 'none' | 'available' | 'downloading' | 'restarting' | 'error' | 'unsupported';

function inTauri(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

/**
 * App updates via the Tauri updater: signed bundles listed in latest.json on the
 * GitHub release. AppImage replaces itself; deb and rpm installs ask for the admin
 * password (pkexec). Only meaningful inside the desktop shell.
 */
class UpdateState {
  status = $state<Status>('idle');
  version = $state('');
  notes = $state('');
  current = $state('');
  error = $state('');
  downloaded = $state(0);
  total = $state(0);
  dismissed = $state(false);

  private update: Update | null = null;

  get supported(): boolean {
    return inTauri();
  }

  async check(): Promise<void> {
    if (!inTauri()) {
      this.status = 'unsupported';
      return;
    }
    if (this.status === 'checking' || this.status === 'downloading') return;
    this.status = 'checking';
    this.error = '';
    try {
      const { getVersion } = await import('@tauri-apps/api/app');
      this.current = await getVersion();
      const { check } = await import('@tauri-apps/plugin-updater');
      this.update = await check();
      if (this.update) {
        this.version = this.update.version;
        this.notes = this.update.body ?? '';
        this.status = 'available';
        this.dismissed = false;
      } else {
        this.status = 'none';
      }
    } catch (e) {
      this.status = 'error';
      this.error = e instanceof Error ? e.message : String(e);
    }
  }

  /** Download, verify the signature, install, then restart the app. */
  async install(): Promise<void> {
    if (!this.update || this.status === 'downloading') return;
    this.status = 'downloading';
    this.downloaded = 0;
    this.total = 0;
    try {
      await this.update.downloadAndInstall((ev) => {
        if (ev.event === 'Started') this.total = ev.data.contentLength ?? 0;
        else if (ev.event === 'Progress') this.downloaded += ev.data.chunkLength;
      });
      this.status = 'restarting';
      const { invoke } = await import('@tauri-apps/api/core');
      await invoke('restart_app');
    } catch (e) {
      this.status = 'error';
      this.error = e instanceof Error ? e.message : String(e);
    }
  }
}

export const updateState = new UpdateState();
