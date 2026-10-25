// The guest link, read from the page's own address. Only the fragment carries the key (it never
// reaches a server).
//
// - format 1 (0.24.0): `https://<host>/n/<room>#<key>`; the page is served by the publish host
//   (or the backend) that also runs the room at `/live/<room>`.
// - format 2 (phase 10): `#2.<room>.<key>.<endpoint id>.<relay host[:port]>`, everything in the
//   fragment so any static origin can serve the page (GitHub Pages: `/mnemosyne/n/`). The room is
//   on the owner's computer, reached over iroh through that one relay. The path may still carry
//   `/n/<room>` (a host mirror or a tunnel serves the page): then the host's WebSocket is the
//   fallback. The relay host contains dots, so the fragment splits at the first four only.
import { ROOM_HEX_LEN, keyFromFragment } from './format';

export interface GuestLink {
  format: 1 | 2;
  room: string;
  key: Uint8Array;
  /** The room endpoint's id (format 2). */
  endpoint?: string;
  /** The relay as `host[:port]`, lowercased (format 2); `relayUrl` makes it a URL. */
  relay?: string;
  /** The path is `/n/<room>` on this origin: the origin runs the room at `/live/<room>`. */
  hostPath: boolean;
}

const ROOM_RE = new RegExp(`^[0-9a-f]{${ROOM_HEX_LEN}}$`);
/** `/n/<room>` at the origin's root, where a host serves the page beside `/live/<room>`. */
const HOST_PATH_RE = new RegExp(`^/n/([0-9a-f]{${ROOM_HEX_LEN}})/?$`);
/** `.../n/<room>` anywhere in the path (format 2 only checks that it names the same room). */
const ANY_ROOM_PATH_RE = new RegExp(`(?:^|/)n/([0-9a-f]{${ROOM_HEX_LEN}})/?$`);
/** An iroh endpoint id: hex (what the sidecar prints) or base32, both accepted by iroh. */
const ENDPOINT_RE = /^(?:[0-9a-f]{64}|[a-z2-7]{52})$/i;
const LABEL = '[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?';
const RELAY_RE = new RegExp(`^(?:\\[[0-9a-f:.]+\\]|${LABEL}(?:\\.${LABEL})*)(?::(\\d{1,5}))?$`);

/** Hosts whose relay is plain HTTP (the e2e dev relay). */
const LOOPBACK = new Set(['127.0.0.1', 'localhost', '[::1]']);

/** The link in `loc`, or null when it is incomplete or not a guest link. */
export function parseLink(loc: { pathname: string; hash: string }): GuestLink | null {
  const frag = loc.hash.replace(/^#/, '');
  if (frag.startsWith('2.')) return parseFormat2(loc.pathname, frag.slice(2));
  const m = HOST_PATH_RE.exec(loc.pathname);
  if (!m) return null;
  const key = readKey(frag);
  return key ? { format: 1, room: m[1], key, hostPath: true } : null;
}

function parseFormat2(pathname: string, rest: string): GuestLink | null {
  const parts: string[] = [];
  for (let i = 0; i < 3; i++) {
    const dot = rest.indexOf('.');
    if (dot < 0) return null;
    parts.push(rest.slice(0, dot));
    rest = rest.slice(dot + 1);
  }
  const [room, keyText, endpoint] = parts;
  const relay = readRelay(rest);
  if (!ROOM_RE.test(room) || !ENDPOINT_RE.test(endpoint) || !relay) return null;
  const key = readKey(keyText);
  if (!key) return null;
  const inPath = ANY_ROOM_PATH_RE.exec(pathname);
  if (inPath && inPath[1] !== room) return null;
  const host = HOST_PATH_RE.exec(pathname);
  return { format: 2, room, key, endpoint, relay, hostPath: host !== null && host[1] === room };
}

function readKey(text: string): Uint8Array | null {
  try {
    return keyFromFragment(text);
  } catch {
    return null;
  }
}

function readRelay(text: string): string | null {
  let host = text;
  if (host.includes('%')) {
    try {
      host = decodeURIComponent(host);
    } catch {
      return null;
    }
  }
  host = host.toLowerCase();
  const m = RELAY_RE.exec(host);
  if (!m) return null;
  if (m[1] !== undefined) {
    const port = Number(m[1]);
    if (port < 1 || port > 65535) return null;
  }
  return host;
}

/** The relay's hostname without the port (`[::1]` keeps its brackets). */
function hostname(host: string): string {
  if (host.startsWith('[')) return host.slice(0, host.indexOf(']') + 1);
  const colon = host.indexOf(':');
  return colon < 0 ? host : host.slice(0, colon);
}

/** The relay URL for iroh: `https://<host>`, plain `http://` on loopback (the e2e dev relay). */
export function relayUrl(host: string): string {
  return `${LOOPBACK.has(hostname(host.toLowerCase())) ? 'http' : 'https'}://${host}`;
}
