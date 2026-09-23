/**
 * Which backend this UI talks to. Stored per browser/webview in localStorage,
 * because in server mode the settings live on the remote backend itself.
 */
const KEY = 'mnemosyne.connection';
export const LOCAL_BACKEND = 'http://127.0.0.1:8008';

interface Stored {
  url: string;
  token: string;
}

function load(): Stored {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) {
      const p = JSON.parse(raw);
      if (typeof p.url === 'string' && p.url) return { url: p.url.replace(/\/+$/, ''), token: p.token ?? '' };
    }
  } catch {
    /* no storage */
  }
  return { url: LOCAL_BACKEND, token: '' };
}

class ConnectionState {
  url = $state(LOCAL_BACKEND);
  token = $state('');
  /** From /health: hostname of the backend we reached and whether it wants a token. */
  host = $state<string | null>(null);
  authRequired = $state(false);

  constructor() {
    const s = load();
    this.url = s.url;
    this.token = s.token;
  }

  get isLocal(): boolean {
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

  save(url: string, token: string) {
    this.url = url.trim().replace(/\/+$/, '') || LOCAL_BACKEND;
    this.token = token.trim();
    try {
      localStorage.setItem(KEY, JSON.stringify({ url: this.url, token: this.token }));
    } catch {
      /* ignore */
    }
  }

  useLocal() {
    this.save(LOCAL_BACKEND, '');
  }
}

export const connectionState = new ConnectionState();
