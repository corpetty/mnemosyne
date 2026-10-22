// NotesDocState against a fake relay that speaks /ws/notes/{id} over a fake WebSocket class.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ORIGIN, WasmHashWeb, view } from './test-harness.js';
import { insertText, setLink } from './commands.js';
import { ensureTerminated, lines, openDoc, type Doc } from './model.js';
import { ARTIFACT_TAG, DELTA_TAG, drain, openFromHello } from './sync.js';
import {
  DEAD_MS,
  NOTES_CLOSE,
  NotesDocState,
  PING_MS,
  RECONNECT_MS,
  TYPING_IDLE_MS,
  type NotesSocket
} from './doc.svelte.js';

const URL = 'https://example.com/long/enough/to/be/an/artifact';

class FakeSocket implements NotesSocket {
  readyState = 0;
  binaryType = '';
  sent: (string | Uint8Array)[] = [];
  onopen: NotesSocket['onopen'] = null;
  onmessage: NotesSocket['onmessage'] = null;
  onclose: NotesSocket['onclose'] = null;
  onerror: NotesSocket['onerror'] = null;
  closedByClient: number | null = null;

  constructor(
    public url: string,
    private server: FakeServer
  ) {
    server.sockets.push(this);
  }

  send(data: string | Uint8Array): void {
    if (this.readyState !== 1) throw new Error('socket not open');
    this.sent.push(data);
    this.server.fromClient(this, data);
  }

  close(code = 1000): void {
    if (this.readyState >= 2) return;
    this.readyState = 3;
    this.closedByClient = code;
    // A real socket reports the close asynchronously, and only if handlers are still attached.
    queueMicrotask(() => this.onclose?.({ code }));
  }

  /** Server side. */
  json(msg: object): void {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
  binary(frame: Uint8Array): void {
    this.onmessage?.({ data: frame.slice().buffer });
  }
  serverClose(code: number): void {
    this.readyState = 3;
    this.onclose?.({ code });
  }
}

/** What /ws/notes/{id} does, minus the database: a log, an optional snapshot, relaying. */
class FakeServer {
  sockets: FakeSocket[] = [];
  log: Uint8Array[] = []; // frames not folded yet
  snapshot: Uint8Array | null = null;
  received: Uint8Array[] = []; // every binary frame a client sent, in order
  jsonIn: Record<string, unknown>[] = [];
  deaf = false; // true: frames from the client vanish (the network dropped them)
  helloExtra: Record<string, unknown> = {};

  Socket: new (url: string) => FakeSocket;
  constructor() {
    const server = this;
    this.Socket = class extends FakeSocket {
      constructor(url: string) {
        super(url, server);
      }
    };
  }

  get socket(): FakeSocket {
    return this.sockets[this.sockets.length - 1];
  }
  get open(): FakeSocket[] {
    return this.sockets.filter((s) => s.readyState === 1);
  }

  fromClient(_from: FakeSocket, data: string | Uint8Array): void {
    if (this.deaf) return;
    if (typeof data === 'string') {
      this.jsonIn.push(JSON.parse(data));
      return;
    }
    this.received.push(data);
    this.log.push(data);
  }

  /** Accept the newest socket: open it and send the hello, snapshot and backlog. */
  accept(overrides: Record<string, unknown> = {}): FakeSocket {
    const s = this.socket;
    s.readyState = 1;
    s.onopen?.({});
    s.json({
      type: 'hello',
      notes: ORIGIN,
      you: { id: '', name: 'Ann Lee' },
      read_only: false,
      present: ['Ann Lee'],
      last_edit: null,
      snapshot: this.snapshot !== null,
      backlog: this.log.length,
      ...this.helloExtra,
      ...overrides
    });
    if (overrides.unavailable) return s;
    if (this.snapshot) s.binary(this.snapshot);
    for (const f of this.log) s.binary(f);
    return s;
  }

  /** Another client's frames arrive: logged, relayed to every open socket. */
  push(...frames: Uint8Array[]): void {
    for (const f of frames) {
      this.log.push(f);
      for (const s of this.open) s.binary(f);
    }
  }

  /** The folded document: snapshot of everything, empty log. */
  fold(): void {
    this.snapshot = this.replica().encode();
    this.log = [];
  }

  replica(): WasmHashWeb {
    return openFromHello(this.snapshot, this.log);
  }
  text(): string {
    return this.replica().text(openDoc(this.replica(), ORIGIN).obj);
  }
  view(): string {
    const r = this.replica();
    return view(lines(openDoc(r, ORIGIN)));
  }
  binarySentBy(s: FakeSocket): Uint8Array[] {
    return s.sent.filter((d): d is Uint8Array => typeof d !== 'string');
  }
}

/** Another participant: produces frames the way a real client does. */
class Peer {
  web = new WasmHashWeb();
  doc: Doc = openDoc(this.web, ORIGIN);
  type(at: number, text: string): Uint8Array[] {
    ensureTerminated(this.doc);
    insertText(this.doc, at, text);
    return drain(this.web);
  }
}

function text(s: NotesDocState): string {
  return s.doc!.web.text(s.doc!.obj);
}

function make(server: FakeServer, opts: Partial<ConstructorParameters<typeof NotesDocState>[0]> = {}) {
  return new NotesDocState({ webSocket: server.Socket, url: (id) => `ws://test/ws/notes/${id}`, ...opts });
}

function bytesEqual(a: Uint8Array, b: Uint8Array): boolean {
  return a.length === b.length && a.every((x, i) => x === b[i]);
}

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe('connecting', () => {
  it('hello, snapshot, backlog, live: the document equals the server', () => {
    const server = new FakeServer();
    const peer = new Peer();
    server.push(...peer.type(0, 'first line. '));
    server.fold();
    server.push(...peer.type(12, 'after the fold'));
    expect(server.snapshot).not.toBeNull();
    expect(server.log.length).toBe(1);

    const s = make(server);
    const changes: number[] = [];
    s.onRemoteChange = () => changes.push(s.version);
    expect(s.status).toBe('connecting');
    s.connect('abc');
    expect(server.socket.url).toBe('ws://test/ws/notes/abc');
    expect(server.socket.binaryType).toBe('arraybuffer');
    const sock = server.socket;
    sock.readyState = 1;
    sock.onopen?.({});
    sock.json({
      type: 'hello',
      notes: ORIGIN,
      you: { id: 'u1', name: 'Ann Lee' },
      read_only: false,
      present: ['Ann Lee', 'Bob'],
      last_edit: { by: 'Bob', at: '2026-10-22T10:15:00' },
      snapshot: true,
      backlog: 1
    });
    expect(s.status).toBe('catching_up');
    expect(s.doc).toBeNull();
    sock.binary(server.snapshot!);
    expect(s.status).toBe('catching_up');
    sock.binary(server.log[0]);
    expect(s.status).toBe('live');
    expect(s.you).toEqual({ id: 'u1', name: 'Ann Lee' });
    expect(s.present).toEqual(['Ann Lee', 'Bob']);
    expect(s.lastEdit).toEqual({ by: 'Bob', at: '2026-10-22T10:15:00' });
    expect(s.readOnly).toBe(false);
    expect(text(s)).toBe(server.text());
    expect(text(s)).toContain('first line. after the fold');
    expect(s.version).toBe(1);
    expect(changes).toEqual([1]);
    // Nothing the relay holds goes back.
    vi.advanceTimersByTime(5000);
    s.localChange();
    vi.advanceTimersByTime(5000);
    expect(server.binarySentBy(sock)).toEqual([]);
  });

  it('an empty server gives an empty document, live at once', () => {
    const server = new FakeServer();
    const s = make(server);
    s.connect('abc');
    server.accept();
    expect(s.status).toBe('live');
    expect(s.doc).not.toBeNull();
    expect(text(s)).toBe('');
    expect(s.version).toBe(1);
  });

  it('presence and last_edit update the state', () => {
    const server = new FakeServer();
    const s = make(server);
    s.connect('abc');
    const sock = server.accept();
    sock.json({ type: 'presence', present: ['Ann Lee', 'Bob'], typing: ['Bob'] });
    expect(s.present).toEqual(['Ann Lee', 'Bob']);
    expect(s.typing).toEqual(['Bob']);
    sock.json({ type: 'last_edit', by: 'Bob', at: '2026-10-22T11:00:00' });
    expect(s.lastEdit).toEqual({ by: 'Bob', at: '2026-10-22T11:00:00' });
  });
});

describe('local edits', () => {
  it('are drained and sent after the 200 ms debounce, and at most 1 s after the first change', () => {
    const server = new FakeServer();
    const s = make(server);
    s.connect('abc');
    const sock = server.accept();
    ensureTerminated(s.doc!);
    insertText(s.doc!, 0, 'hello');
    s.localChange();
    vi.advanceTimersByTime(199);
    expect(server.received.length).toBe(0);
    vi.advanceTimersByTime(1);
    expect(server.received.length).toBe(1);
    expect(server.received[0][0]).toBe(DELTA_TAG);
    expect(server.text()).toBe('hello\n');

    // A burst of changes 150 ms apart keeps pushing the debounce back, but not past 1 s.
    server.received.length = 0;
    for (let i = 0; i < 10; i++) {
      insertText(s.doc!, 5 + i, String(i));
      s.localChange();
      vi.advanceTimersByTime(150);
    }
    expect(server.received.length).toBe(1); // the 1 s max wait flushed once
    vi.advanceTimersByTime(200);
    expect(server.received.length).toBe(2);
    expect(server.text()).toBe('hello0123456789\n');
    expect(server.binarySentBy(sock).length).toBe(3);
  });

  it('send artifacts before the delta', () => {
    const server = new FakeServer();
    const s = make(server);
    s.connect('abc');
    server.accept();
    ensureTerminated(s.doc!);
    insertText(s.doc!, 0, 'see');
    setLink(s.doc!, 0, 3, URL);
    s.localChange();
    vi.advanceTimersByTime(200);
    expect(server.received.map((f) => f[0])).toEqual([ARTIFACT_TAG, DELTA_TAG]);
  });
});

describe('remote frames', () => {
  it('are applied, bump version, and are never echoed back', () => {
    const server = new FakeServer();
    const s = make(server);
    s.connect('abc');
    const sock = server.accept();
    const v0 = s.version;
    const calls = vi.fn();
    s.onRemoteChange = calls;
    const peer = new Peer();
    server.push(...peer.type(0, 'from bob'));
    expect(text(s)).toBe('from bob\n');
    expect(s.version).toBe(v0 + 1);
    expect(calls).toHaveBeenCalledTimes(1);
    s.localChange();
    vi.advanceTimersByTime(3000);
    expect(server.binarySentBy(sock)).toEqual([]);

    // A replay delivers nothing: no version bump.
    sock.binary(server.log[0]);
    expect(s.version).toBe(v0 + 1);
  });

  it('send our unsent edits first, then apply (the QUEUE 39 order)', () => {
    const server = new FakeServer();
    const s = make(server);
    s.connect('abc');
    server.accept();
    ensureTerminated(s.doc!);
    insertText(s.doc!, 0, 'mine');
    s.localChange(); // still debouncing when Bob's frame arrives
    const peer = new Peer();
    const frames = peer.type(0, 'bob ');
    server.sockets[0].binary(frames[0]);
    expect(server.received.length).toBe(1); // ours went out right then
    expect(text(s)).toContain('bob');
    expect(text(s)).toContain('mine');
    vi.advanceTimersByTime(1000);
    expect(server.received.length).toBe(1); // and Bob's is not sent back
  });

  it('a new artifact bumps version', () => {
    const server = new FakeServer();
    const s = make(server);
    s.connect('abc');
    const sock = server.accept();
    const peer = new Peer();
    ensureTerminated(peer.doc);
    insertText(peer.doc, 0, 'see');
    setLink(peer.doc, 0, 3, URL);
    const [art, delta] = drain(peer.web);
    const v = s.version;
    sock.binary(art);
    expect(s.version).toBe(v + 1);
    sock.binary(delta);
    expect(s.version).toBe(v + 2);
    expect(view(lines(s.doc!))).toContain(`link=${URL}`);
  });
});

describe('reconnect', () => {
  it('re-sends exactly the ops the server lacks, nothing it had', () => {
    const server = new FakeServer();
    const s = make(server);
    s.connect('abc');
    server.accept();
    ensureTerminated(s.doc!);
    insertText(s.doc!, 0, 'a');
    s.localChange();
    vi.advanceTimersByTime(200); // 'a' reached the server
    expect(server.text()).toBe('a\n');

    // The network drops: 'b' is sent into the void, 'c' is typed after the drop.
    server.deaf = true;
    insertText(s.doc!, 1, 'b');
    s.localChange();
    vi.advanceTimersByTime(200);
    server.socket.serverClose(1006);
    expect(s.status).toBe('reconnecting');
    insertText(s.doc!, 2, 'c');
    s.localChange(); // not live: nothing is drained
    vi.advanceTimersByTime(RECONNECT_MS[0] - 1);
    expect(server.sockets.length).toBe(1);
    vi.advanceTimersByTime(1);
    expect(server.sockets.length).toBe(2);

    // Meanwhile Bob typed, and the server (now hearing us again) folded.
    const peer = new Peer();
    server.deaf = false;
    server.push(...peer.type(0, 'bob:'));
    const before = server.received.length;
    expect(s.status).toBe('reconnecting');
    server.accept();
    expect(s.status).toBe('live');

    const resent = server.received.slice(before);
    expect(resent.map((f) => f[0])).toEqual([DELTA_TAG]);
    // The reference: a replica with 'a' sent, then 'b' and 'c', gives these exact bytes.
    const ref = new WasmHashWeb();
    const refDoc = openDoc(ref, ORIGIN);
    ensureTerminated(refDoc);
    insertText(refDoc, 0, 'a');
    ref.takeDeltas();
    insertText(refDoc, 1, 'b');
    insertText(refDoc, 2, 'c');
    expect(bytesEqual(resent[0], ref.takeDeltas())).toBe(true);

    // Server and client converge on the same text, and nothing is left to send.
    expect(s.doc && text(s)).toBe(server.text());
    expect(text(s)).toContain('bob:');
    expect(text(s)).toContain('abc');
    s.localChange();
    vi.advanceTimersByTime(3000);
    expect(server.received.slice(before).length).toBe(1);
    expect(s.version).toBeGreaterThan(1);
  });

  it('works through a folded snapshot too, and keeps the same store', () => {
    const server = new FakeServer();
    const s = make(server);
    s.connect('abc');
    server.accept();
    const web = s.doc!.web;
    ensureTerminated(s.doc!);
    insertText(s.doc!, 0, 'one ');
    s.localChange();
    vi.advanceTimersByTime(200);
    server.fold();
    const peer = new Peer();
    server.push(...peer.type(0, 'two '));
    const sock = server.socket;
    server.deaf = true;
    insertText(s.doc!, 4, 'three');
    s.localChange();
    vi.advanceTimersByTime(200); // lost
    sock.serverClose(1006);
    vi.advanceTimersByTime(RECONNECT_MS[0]);
    server.deaf = false;
    server.accept(); // snapshot + backlog (Bob's frame, plus the earlier 'two ' since never folded)
    expect(s.doc!.web).toBe(web);
    expect(s.status).toBe('live');
    expect(text(s)).toBe(server.text());
    expect(text(s)).toContain('three');
  });

  it('sends every artifact again, even one the server got', () => {
    const server = new FakeServer();
    const s = make(server);
    s.connect('abc');
    server.accept();
    ensureTerminated(s.doc!);
    insertText(s.doc!, 0, 'see');
    setLink(s.doc!, 0, 3, URL);
    s.localChange();
    vi.advanceTimersByTime(200);
    expect(server.received.map((f) => f[0])).toEqual([ARTIFACT_TAG, DELTA_TAG]);

    // Second link, lost in the drop.
    insertText(s.doc!, 3, ' more');
    setLink(s.doc!, 4, 8, URL + '/second/one/also/long');
    server.deaf = true;
    s.localChange();
    vi.advanceTimersByTime(200);
    server.socket.serverClose(1006);
    vi.advanceTimersByTime(RECONNECT_MS[0]);
    server.deaf = false;
    const before = server.received.length;
    server.accept();
    const resent = server.received.slice(before);
    // Both artifacts (duplicates are harmless), first, then the missing delta.
    expect(resent.map((f) => f[0])).toEqual([ARTIFACT_TAG, ARTIFACT_TAG, DELTA_TAG]);
    const v = server.view();
    expect(v).toContain(`link=${URL}`);
    expect(v).toContain(`link=${URL}/second/one/also/long`);
    expect(v).toBe(view(lines(s.doc!)));
  });

  it('does not reconnect after destroy, and destroy cancels pending timers', () => {
    const server = new FakeServer();
    const s = make(server);
    s.connect('abc');
    server.accept();
    ensureTerminated(s.doc!);
    insertText(s.doc!, 0, 'x');
    s.localChange();
    s.destroy();
    vi.advanceTimersByTime(60_000);
    expect(server.sockets.length).toBe(1);
    expect(server.received.length).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe('wrapRemote', () => {
  it('wraps a remote frame and a reconnect merge, which run once and synchronously inside it', () => {
    const server = new FakeServer();
    const seen: string[] = [];
    const s = make(server, {
      wrapRemote: (apply) => {
        seen.push('before:' + text(s));
        apply();
        seen.push('after:' + text(s));
      }
    });
    s.connect('abc');
    server.accept(); // the first document is built, not wrapped (no editor yet)
    expect(seen).toEqual([]);
    const peer = new Peer();
    server.push(...peer.type(0, 'hi'));
    expect(seen).toEqual(['before:', 'after:hi\n']);
    // A reconnect that brings Bob's later typing.
    server.socket.serverClose(1006);
    server.push(...peer.type(2, '!'));
    vi.advanceTimersByTime(RECONNECT_MS[0]);
    seen.length = 0;
    server.accept();
    expect(seen).toEqual(['before:hi\n', 'after:hi!\n']);
  });
});

describe('applyLocal', () => {
  it('runs inside the wrapper, sends like a typed change and tells the editor', () => {
    const server = new FakeServer();
    const seen: string[] = [];
    const s = make(server, { wrapRemote: (apply) => (seen.push('wrap'), apply()) });
    expect(s.applyLocal(() => 1)).toBeUndefined(); // no document yet
    s.connect('abc');
    server.accept();
    const changes: number[] = [];
    s.onRemoteChange = () => changes.push(s.version);
    const v = s.version;
    const out = s.applyLocal((doc) => {
      ensureTerminated(doc);
      insertText(doc, 0, 'added');
      return 'ok';
    });
    expect(out).toBe('ok');
    expect(seen).toEqual(['wrap']);
    expect(changes).toEqual([v + 1]);
    expect(server.received.length).toBe(0); // debounced
    vi.advanceTimersByTime(200);
    expect(server.text()).toBe('added\n');
  });
});

describe('reconnect()', () => {
  it('the relay\'s reload rejoins at once, keeping the document and edits typed meanwhile', () => {
    const server = new FakeServer();
    const s = make(server);
    s.connect('abc');
    const first = server.accept();
    ensureTerminated(s.doc!);
    insertText(s.doc!, 0, 'a');
    s.localChange();
    vi.advanceTimersByTime(200);
    expect(server.text()).toBe('a\n');
    first.json({ type: 'reload' });
    expect(s.status).toBe('reconnecting');
    expect(server.sockets.length).toBe(2); // no backoff
    first.serverClose(1000); // the old socket's close is ignored
    expect(server.sockets.length).toBe(2);
    insertText(s.doc!, 1, 'b');
    s.localChange(); // not live: kept for the rejoin
    server.accept();
    expect(s.status).toBe('live');
    expect(server.text()).toBe('ab\n');
    expect(text(s)).toBe('ab\n');
  });

  it('a meeting waiting for its document takes it on reconnect and becomes editable', () => {
    const server = new FakeServer();
    const s = make(server);
    s.connect('abc');
    server.accept({ read_only: true, waiting_for_sync: true });
    expect(s.waitingForSync).toBe(true);
    expect(s.readOnly).toBe(true);
    const peer = new Peer();
    server.push(...peer.type(0, 'arrived'));
    server.fold();
    s.reconnect();
    server.accept();
    expect(s.waitingForSync).toBe(false);
    expect(s.readOnly).toBe(false);
    expect(text(s)).toContain('arrived');
  });

  it('restarts after an error, but not after destroy', () => {
    const server = new FakeServer();
    const s = make(server);
    s.connect('abc');
    server.accept();
    server.socket.serverClose(NOTES_CLOSE.badFrame);
    expect(s.status).toBe('error');
    s.reconnect();
    expect(s.status).toBe('reconnecting');
    expect(s.error).toBeNull();
    expect(server.sockets.length).toBe(2);
    server.accept();
    expect(s.status).toBe('live');
    s.destroy();
    s.reconnect();
    expect(server.sockets.length).toBe(2);
  });
});

describe('closes that end the connection', () => {
  it('unavailable: no document, no reconnect', async () => {
    const server = new FakeServer();
    const s = make(server);
    s.connect('abc');
    const sock = server.accept({ unavailable: true });
    expect(s.status).toBe('unavailable');
    expect(s.doc).toBeNull();
    sock.serverClose(1000);
    vi.advanceTimersByTime(60_000);
    expect(server.sockets.length).toBe(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('4404 gone, 4403 read-only edit refused, 4400 and 1009: no reconnect', () => {
    for (const [code, status] of [
      [NOTES_CLOSE.gone, 'gone'],
      [NOTES_CLOSE.readOnly, 'error'],
      [NOTES_CLOSE.badFrame, 'error'],
      [NOTES_CLOSE.tooBig, 'error']
    ] as const) {
      const server = new FakeServer();
      const s = make(server);
      s.connect('abc');
      server.accept();
      server.socket.serverClose(code);
      expect(s.status).toBe(status);
      vi.advanceTimersByTime(60_000);
      expect(server.sockets.length).toBe(1);
      if (status === 'error') expect(s.error).toBeTruthy();
      s.destroy();
    }
  });

  it('a close before any hello retries with the 1, 2, 5, 10, 10 s backoff', () => {
    const server = new FakeServer();
    const s = make(server);
    s.connect('abc');
    expect(s.status).toBe('connecting');
    let expected = 1;
    for (const wait of [...RECONNECT_MS, RECONNECT_MS[3]]) {
      server.socket.serverClose(1006);
      expect(s.status).toBe('connecting');
      vi.advanceTimersByTime(wait - 1);
      expect(server.sockets.length).toBe(expected);
      vi.advanceTimersByTime(1);
      expect(server.sockets.length).toBe(++expected);
    }
    // A completed hello resets the backoff.
    server.accept();
    server.socket.serverClose(1006);
    expect(s.status).toBe('reconnecting');
    vi.advanceTimersByTime(RECONNECT_MS[0]);
    expect(server.sockets.length).toBe(expected + 1);
  });

  it('a drop in the middle of catching up discards the partial state and starts over', () => {
    const server = new FakeServer();
    const peer = new Peer();
    server.push(...peer.type(0, 'abc'));
    server.push(...peer.type(3, 'def'));
    const s = make(server);
    s.connect('abc');
    const sock = server.socket;
    sock.readyState = 1;
    sock.json({
      type: 'hello', notes: ORIGIN, you: { id: '', name: 'A' }, read_only: false,
      present: ['A'], last_edit: null, snapshot: false, backlog: 2
    });
    sock.binary(server.log[0]);
    sock.serverClose(1006);
    expect(s.doc).toBeNull();
    vi.advanceTimersByTime(RECONNECT_MS[0]);
    server.accept();
    expect(s.status).toBe('live');
    expect(text(s)).toBe('abcdef\n');
  });
});

describe('read-only', () => {
  it('never sends a frame or presence, but applies remote ones', () => {
    const server = new FakeServer();
    const s = make(server);
    s.connect('abc');
    const sock = server.accept({ read_only: true });
    expect(s.readOnly).toBe(true);
    expect(s.waitingForSync).toBe(false);
    ensureTerminated(s.doc!);
    insertText(s.doc!, 0, 'local');
    s.localChange();
    s.flush();
    s.setTyping(true);
    vi.advanceTimersByTime(5000);
    expect(server.binarySentBy(sock)).toEqual([]);
    expect(server.jsonIn.filter((m) => m.type === 'presence')).toEqual([]);
    const peer = new Peer();
    server.push(...peer.type(0, 'bob'));
    expect(text(s)).toContain('bob');
    expect(server.binarySentBy(sock)).toEqual([]);
  });

  it('stays read-only across a reconnect, with local content never sent', () => {
    const server = new FakeServer();
    const s = make(server);
    s.connect('abc');
    server.accept({ read_only: true });
    ensureTerminated(s.doc!);
    insertText(s.doc!, 0, 'local');
    server.socket.serverClose(1006);
    vi.advanceTimersByTime(RECONNECT_MS[0]);
    const sock = server.accept({ read_only: true });
    expect(server.binarySentBy(sock)).toEqual([]);
  });

  it('waiting_for_sync is read-only with its own flag', () => {
    const server = new FakeServer();
    const s = make(server);
    s.connect('abc');
    server.accept({ read_only: true, waiting_for_sync: true });
    expect(s.status).toBe('live');
    expect(s.readOnly).toBe(true);
    expect(s.waitingForSync).toBe(true);
    expect(s.doc).not.toBeNull();
    // Even if the server forgot read_only, waiting means read-only.
    const t = make(server);
    t.connect('abc');
    server.accept({ waiting_for_sync: true });
    expect(t.readOnly).toBe(true);
  });
});

describe('typing', () => {
  it('tells the server once, refreshes quietly, and sends false after 2 s idle', () => {
    const server = new FakeServer();
    const s = make(server);
    s.connect('abc');
    server.accept();
    const presence = () => server.jsonIn.filter((m) => m.type === 'presence');
    for (let i = 0; i < 5; i++) {
      s.setTyping(true);
      vi.advanceTimersByTime(300);
    }
    expect(presence()).toEqual([{ type: 'presence', typing: true }]);
    vi.advanceTimersByTime(TYPING_IDLE_MS - 300 - 1);
    expect(presence().length).toBe(1);
    vi.advanceTimersByTime(1);
    expect(presence()).toEqual([
      { type: 'presence', typing: true },
      { type: 'presence', typing: false }
    ]);
    // Typing again starts a new burst; an explicit false goes out at once, and only once.
    s.setTyping(true);
    s.setTyping(false);
    s.setTyping(false);
    expect(presence().map((m) => m.typing)).toEqual([true, false, true, false]);
    vi.advanceTimersByTime(10_000);
    expect(presence().length).toBe(4);
  });

  it('does nothing while the socket is not open', () => {
    const server = new FakeServer();
    const s = make(server);
    s.connect('abc');
    s.setTyping(true); // no doc yet
    server.accept();
    server.socket.serverClose(1006);
    s.setTyping(true);
    expect(server.jsonIn).toEqual([]);
  });
});

describe('heartbeat', () => {
  it('pings every 25 s and stays up while pongs come', () => {
    const server = new FakeServer();
    const s = make(server);
    s.connect('abc');
    const sock = server.accept();
    const pings = () => server.jsonIn.filter((m) => m.type === 'ping').length;
    for (let i = 1; i <= 8; i++) {
      vi.advanceTimersByTime(PING_MS);
      expect(pings()).toBe(i);
      sock.json({ type: 'pong' });
    }
    expect(server.sockets.length).toBe(1);
    expect(s.status).toBe('live');
  });

  it('a socket that stops answering is dropped and reconnected', () => {
    const server = new FakeServer();
    const s = make(server);
    s.connect('abc');
    const sock = server.accept();
    // No pongs. The silence reaches DEAD_MS at the third tick (75 s).
    vi.advanceTimersByTime(PING_MS * 2);
    expect(s.status).toBe('live');
    vi.advanceTimersByTime(PING_MS);
    expect(PING_MS * 3).toBeGreaterThanOrEqual(DEAD_MS);
    expect(s.status).toBe('reconnecting');
    expect(sock.closedByClient).not.toBeNull();
    expect(server.sockets.length).toBe(1);
    vi.advanceTimersByTime(RECONNECT_MS[0]);
    expect(server.sockets.length).toBe(2);
    server.accept();
    expect(s.status).toBe('live');
  });

  it('any message from the relay counts as alive', () => {
    const server = new FakeServer();
    const s = make(server);
    s.connect('abc');
    const sock = server.accept();
    for (let i = 0; i < 6; i++) {
      vi.advanceTimersByTime(PING_MS);
      sock.json({ type: 'presence', present: ['Ann Lee'], typing: [] });
    }
    expect(s.status).toBe('live');
    expect(server.sockets.length).toBe(1);
  });
});
