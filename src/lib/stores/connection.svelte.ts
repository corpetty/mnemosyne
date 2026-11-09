/**
 * Which backend this UI talks to. Stored per browser/webview in localStorage,
 * because in server mode the settings live on the remote backend itself.
 */
import type { Me } from '$lib/types/index.js';

const KEY = 'mnemosyne.connection';
/** The token of this computer's own backend, kept apart from the connection above: that one holds
 * whatever backend the window is connected to now (a main computer through remote access), and
 * leaving it must not lose the token the local backend generated for itself (backend
 * services/pairing_prep.py: the person never saw it). */
const LOCAL_KEY = 'mnemosyne.connection.local';
export const LOCAL_BACKEND = 'http://127.0.0.1:8008';
const LOCAL_URL = /^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/;

/** Served by a backend itself (api/web.py, e.g. a team server opened in a browser): that
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

function loadLocalToken(): string {
  try {
    const raw = localStorage.getItem(LOCAL_KEY);
    const p = raw ? JSON.parse(raw) : null;
    return p && typeof p.token === 'string' ? p.token : '';
  } catch {
    return '';
  }
}

class ConnectionState {
  url = $state(LOCAL_BACKEND);
  token = $state('');
  remote = $state(false);
  /** From /health: hostname of the backend we reached and whether it wants a token. */
  host = $state<string | null>(null);
  authRequired = $state(false);
  /** Each recording asks how its people agreed to it (from /health). */
  consentRequired = $state(false);
  /** Who is signed in on a team server (empty id: the desktop app or the admin token). */
  me = $state<Me | null>(null);
  /** The token of this computer's own backend (see LOCAL_KEY); '' when it has none. */
  localToken = $state('');

  constructor() {
    const s = load();
    this.url = s.url;
    this.remote = s.remote ?? false;
    const onLocal = !this.remote && !SAME_ORIGIN && LOCAL_URL.test(this.url);
    // A window from before LOCAL_KEY keeps the token it had for the local backend.
    this.localToken = loadLocalToken() || (onLocal ? s.token : '');
    this.token = onLocal && !s.token ? this.localToken : s.token;
  }

  get isLocal(): boolean {
    // The remote-access tunnel listens on 127.0.0.1, but the backend is on another machine.
    if (this.remote) return false;
    return this.url === LOCAL_BACKEND || LOCAL_URL.test(this.url);
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
    // Saving for this computer's own backend (also a token the person typed for it) keeps it.
    if (!remote && !SAME_ORIGIN && LOCAL_URL.test(this.url)) this.localToken = this.token;
    try {
      localStorage.setItem(KEY, JSON.stringify({ url: this.url, token: this.token, remote }));
      localStorage.setItem(LOCAL_KEY, JSON.stringify({ token: this.localToken }));
    } catch {
      /* ignore */
    }
  }

  /** Back to this computer's own backend, with the token it has. */
  useLocal() {
    this.save(defaultUrl(), SAME_ORIGIN ? '' : this.localToken);
  }
}

export const connectionState = new ConnectionState();
