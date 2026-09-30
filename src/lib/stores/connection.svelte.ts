/**
 * Which backend this UI talks to. Stored per browser/webview in localStorage,
 * because in server mode the settings live on the remote backend itself.
 */
const KEY = 'mnemosyne.connection';
export const LOCAL_BACKEND = 'http://127.0.0.1:8008';

/** Served by a backend itself (api/web.py, e.g. a firm's server opened in a browser): that
 * server is the backend, not this computer's. */
export const SAME_ORIGIN =
  typeof document !== 'undefined' &&
  document.querySelector('meta[name="mnemosyne-backend"][content="same-origin"]') !== null;

function defaultUrl(): string {
  return SAME_ORIGIN ? location.origin : LOCAL_BACKEND;
}

interface Stored {
  url: string;
  token: string;
  /** Through this computer's remote-access tunnel (desktop app, src-tauri/src/remote.rs). */
  remote?: boolean;
}

function load(): Stored {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) {
      const p = JSON.parse(raw);
      if (typeof p.url === 'string' && p.url)
        return { url: p.url.replace(/\/+$/, ''), token: p.token ?? '', remote: p.remote === true };
    }
  } catch {
    /* no storage */
  }
  return { url: defaultUrl(), token: '' };
}

class ConnectionState {
  url = $state(LOCAL_BACKEND);
  token = $state('');
  remote = $state(false);
  /** From /health: hostname of the backend we reached and whether it wants a token. */
  host = $state<string | null>(null);
  authRequired = $state(false);

  constructor() {
    const s = load();
    this.url = s.url;
    this.token = s.token;
    this.remote = s.remote ?? false;
  }

  get isLocal(): boolean {
    // The remote-access tunnel listens on 127.0.0.1, but the backend is on another machine.
    if (this.remote) return false;
    return this.url === LOCAL_BACKEND || /^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(this.url);
  }

  get wsUrl(): string {
    const ws = this.url.replace(/^http/, 'ws') + '/ws';
    return this.token ? `${ws}?token=${encodeURIComponent(this.token)}` : ws;
  }

  headers(): Record<string, string> {
    return this.token ? { Authorization: `Bearer ${this.token}` } : {};
  }

  /** Append the token as a query param for URLs used by <audio>/<a> elements. */
  withToken(url: string): string {
    if (!this.token) return url;
    return url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(this.token);
  }

  save(url: string, token: string, remote = false) {
    this.url = url.trim().replace(/\/+$/, '') || defaultUrl();
    this.token = token.trim();
    this.remote = remote;
    try {
      localStorage.setItem(KEY, JSON.stringify({ url: this.url, token: this.token, remote }));
    } catch {
      /* ignore */
    }
  }

  useLocal() {
    this.save(defaultUrl(), '');
  }
}

export const connectionState = new ConnectionState();
