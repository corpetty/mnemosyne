// Guest links, format 1 (`/n/<room>#<key>`) and format 2 (`#2.<room>.<key>.<endpoint>.<relay>`).
import { describe, expect, it } from 'vitest';
import { keyToFragment } from './format';
import { parseLink, relayUrl } from './link';

const key = new Uint8Array(32).map((_, i) => (i * 37 + 11) & 0xff);
const KEY = keyToFragment(key);
const ROOM = '0123456789abcdef';
const EP = 'ab'.repeat(32);

const at = (pathname: string, hash: string) => ({ pathname, hash });

describe('format 1', () => {
  it('reads the room from the path and the key from the fragment', () => {
    const l = parseLink(at(`/n/${ROOM}`, `#${KEY}`));
    expect(l).toMatchObject({ format: 1, room: ROOM, hostPath: true });
    expect(l?.key).toEqual(key);
    expect(l?.endpoint).toBeUndefined();
    expect(parseLink(at(`/n/${ROOM}/`, `#${KEY}`))?.room).toBe(ROOM);
  });

  it('refuses a missing or bad room or key', () => {
    expect(parseLink(at('/n/', `#${KEY}`))).toBeNull();
    expect(parseLink(at('/n/0123', `#${KEY}`))).toBeNull();
    expect(parseLink(at(`/n/${ROOM.toUpperCase()}`, `#${KEY}`))).toBeNull();
    expect(parseLink(at(`/n/${ROOM}`, ''))).toBeNull();
    expect(parseLink(at(`/n/${ROOM}`, `#${KEY.slice(1)}`))).toBeNull();
    expect(parseLink(at(`/n/${ROOM}`, `#${KEY.slice(1)}=`))).toBeNull();
    // A format 1 link needs its host: no room in the path under a prefix.
    expect(parseLink(at(`/mnemosyne/n/${ROOM}`, `#${KEY}`))).toBeNull();
  });
});

describe('format 2', () => {
  const frag = (relay: string, room = ROOM, ep = EP, k = KEY) => `#2.${room}.${k}.${ep}.${relay}`;

  it('reads everything from the fragment on a static page', () => {
    const l = parseLink(at('/mnemosyne/n/', frag('relay.example.com')));
    expect(l).toMatchObject({ format: 2, room: ROOM, endpoint: EP, relay: 'relay.example.com', hostPath: false });
    expect(l?.key).toEqual(key);
    expect(parseLink(at('/n/', frag('relay.example.com')))?.hostPath).toBe(false);
    expect(parseLink(at('/', frag('relay.example.com')))?.hostPath).toBe(false);
  });

  it('splits at the first four dots only: the relay host keeps its own', () => {
    const l = parseLink(at('/n/', frag('euc1-1.relay.n0.iroh-canary.iroh.link')));
    expect(l?.relay).toBe('euc1-1.relay.n0.iroh-canary.iroh.link');
    expect(parseLink(at('/n/', frag('relay.example.com:8443')))?.relay).toBe('relay.example.com:8443');
    expect(parseLink(at('/n/', frag('127.0.0.1:3340')))?.relay).toBe('127.0.0.1:3340');
    expect(parseLink(at('/n/', frag('[::1]:3340')))?.relay).toBe('[::1]:3340');
    expect(parseLink(at('/n/', frag('%5B::1%5D:3340')))?.relay).toBe('[::1]:3340');
    expect(parseLink(at('/n/', frag('Relay.Example.COM')))?.relay).toBe('relay.example.com');
  });

  it('marks a host path: the page is served beside /live/<room>', () => {
    expect(parseLink(at(`/n/${ROOM}`, frag('relay.example.com')))?.hostPath).toBe(true);
    expect(parseLink(at(`/n/${ROOM}/`, frag('relay.example.com')))?.hostPath).toBe(true);
    // The same room under a prefix: no host behind it at /live/<room>.
    expect(parseLink(at(`/mnemosyne/n/${ROOM}`, frag('relay.example.com')))?.hostPath).toBe(false);
  });

  it('refuses a path that names another room', () => {
    expect(parseLink(at('/n/fedcba9876543210', frag('relay.example.com')))).toBeNull();
  });

  it('accepts a base32 endpoint id', () => {
    const b32 = 'a'.repeat(26) + '234567'.repeat(4) + 'bb';
    expect(b32).toHaveLength(52);
    expect(parseLink(at('/n/', frag('relay.example.com', ROOM, b32)))?.endpoint).toBe(b32);
  });

  it('refuses bad links', () => {
    const bad = [
      '#2.',
      `#2.${ROOM}`,
      `#2.${ROOM}.${KEY}`,
      `#2.${ROOM}.${KEY}.${EP}`,
      `#2.${ROOM}.${KEY}.${EP}.`,
      frag('relay.example.com', 'xyz'),
      frag('relay.example.com', ROOM, 'ab'.repeat(31)),
      frag('relay.example.com', ROOM, EP, KEY.slice(2)),
      frag('relay.example.com/path'),
      frag('user@relay.example.com'),
      frag('relay.example.com:0'),
      frag('relay.example.com:70000'),
      frag('relay..example.com'),
      frag('-relay.example.com'),
      frag('relay.example.com.'),
      frag('relay example.com'),
      frag('%E0%A4%A'),
      `#3.${ROOM}.${KEY}.${EP}.relay.example.com`
    ];
    for (const hash of bad) expect(parseLink(at('/n/', hash)), hash).toBeNull();
  });
});

describe('relayUrl', () => {
  it('is https, except on loopback (the e2e dev relay is plain HTTP)', () => {
    expect(relayUrl('relay.example.com')).toBe('https://relay.example.com');
    expect(relayUrl('relay.example.com:8443')).toBe('https://relay.example.com:8443');
    expect(relayUrl('127.0.0.1:3340')).toBe('http://127.0.0.1:3340');
    expect(relayUrl('localhost:3340')).toBe('http://localhost:3340');
    expect(relayUrl('localhost')).toBe('http://localhost');
    expect(relayUrl('[::1]:3340')).toBe('http://[::1]:3340');
    expect(relayUrl('127.0.0.2:3340')).toBe('https://127.0.0.2:3340');
    expect(relayUrl('localhost.example.com')).toBe('https://localhost.example.com');
  });
});
