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
/** The team's library: this computer's backend proxies the team it joined under this path (backend
 * api/routes/team_member.py). The window sends its OWN token; the backend adds the member's. */
export const TEAM_URL = `${LOCAL_BACKEND}/api/joined`;
/** Which library the window shows: this computer's own, or the team's. */
export type Library = 'mine' | 'team';
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
    // The team library is this computer's backend too: it takes this computer's token.
    this.token = this.inTeam ? this.localToken : onLocal && !s.token ? this.localToken : s.token;
  }

  /** Which library this window shows. It is the connection's address, so it is saved with it and
   * survives a reload: the team's library is the backend's `/api/joined` proxy. */
  get library(): Library {
    return !SAME_ORIGIN && !this.remote && this.url === TEAM_URL ? 'team' : 'mine';
  }

  /** The window shows the library of the team this computer joined. */
  get inTeam(): boolean {
    return this.library === 'team';
  }

  /** Requests for this computer's own devices (recording sources, levels, echo cancellation) go to
   * the local backend: the window is connected to another computer's library (a main computer
   * through remote access, or the team's), and its devices are not the ones to record from. */
  get devicesAreLocal(): boolean {
    return this.remote || this.inTeam;
  }

  /** Personal features (contacts, sharing with people) are this caller's: everyone on a plain
   * computer, only the owner on one that hosts a team, nobody on a dedicated team server. */
  get ownsPersonal(): boolean {
    const me = this.me;
    return !me?.team_mode || (me.personal && me.owner);
  }

  /** The owner of this computer is asking, however it hosts a team (members and reviewers never). */
  get isOwner(): boolean {
    return !this.me?.team_mode || !!this.me.owner;
  }

  get isLocal(): boolean {
    // The remote-access tunnel listens on 127.0.0.1, but the backend is on another machine; the
    // team's library is another computer's too, though reached through this one.
    if (this.remote || this.inTeam) return false;
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

  /** This computer uses a main computer: the window is connected to it through remote access.
   * Recording then goes through this computer's own backend (api/local.ts, stores/satellite). */
  get usesMain(): boolean {
    return this.remote;
  }

  /** The local backend made a token for itself while the window is connected elsewhere
   * (pairing prepare): keep it, the next request to it needs it. */
  setLocalToken(token: string) {
    this.localToken = token.trim();
    const onLocal = !this.remote && !SAME_ORIGIN && (LOCAL_URL.test(this.url) || this.inTeam);
    if (onLocal) this.token = this.localToken;
    try {
      localStorage.setItem(LOCAL_KEY, JSON.stringify({ token: this.localToken }));
      if (onLocal) localStorage.setItem(KEY, JSON.stringify({ url: this.url, token: this.token, remote: false }));
    } catch {
      /* ignore */
    }
  }

  /** Back to this computer's own backend, with the token it has. */
  useLocal() {
    this.save(defaultUrl(), SAME_ORIGIN ? '' : this.localToken);
  }

  /** Show the team's library (or this computer's own): saves the choice, the caller reconnects.
   * Both are this computer's backend with this computer's token. */
  useLibrary(library: Library) {
    if (library === 'team') this.save(TEAM_URL, this.localToken);
    else this.useLocal();
  }
}

export const connectionState = new ConnectionState();
