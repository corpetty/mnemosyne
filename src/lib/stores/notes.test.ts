// The live-notes registry: ref counting, one state per meeting, the bus event.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import '../notes/test-harness.js'; // loads the wasm
import type { NotesSocket } from '../notes/doc.svelte.js';
import { acquireNotes, peekNotes, releaseNotes } from './notes.svelte.js';
import { wsState } from './websocket.svelte.js';

class Sock implements NotesSocket {
  static all: Sock[] = [];
  readyState = 0;
  binaryType = '';
  closedWith: number | null = null;
  onopen: NotesSocket['onopen'] = null;
  onmessage: NotesSocket['onmessage'] = null;
  onclose: NotesSocket['onclose'] = null;
  onerror: NotesSocket['onerror'] = null;
  constructor(public url: string) {
    Sock.all.push(this);
  }
  send(): void {}
  close(code = 1000): void {
    this.readyState = 3;
    this.closedWith = code;
  }
  hello(extra: Record<string, unknown> = {}): void {
    this.readyState = 1;
    this.onopen?.({});
    this.onmessage?.({
      data: JSON.stringify({
        type: 'hello',
        notes: 'a1'.repeat(32),
        you: { id: '', name: 'Ann' },
        read_only: false,
        present: ['Ann'],
        last_edit: null,
        snapshot: false,
        backlog: 0,
        ...extra
      })
    });
  }
}

const opts = { webSocket: Sock, url: (id: string) => `ws://test/${id}` };
const flush = () => new Promise((r) => setTimeout(r, 0));
const bus = (msg: Record<string, unknown>) =>
  (wsState as unknown as { handlers: ((m: Record<string, unknown>) => void)[] }).handlers.forEach((h) => h(msg));
const handlers = () => (wsState as unknown as { handlers: unknown[] }).handlers.length;

beforeEach(() => {
  Sock.all = [];
});
afterEach(() => {
  releaseNotes('m1');
  releaseNotes('m1');
  releaseNotes('m2');
});

describe('acquireNotes / releaseNotes', () => {
  it('shares one state and one connection per meeting, and destroys on the last release', async () => {
    const a = acquireNotes('m1', opts);
    const b = acquireNotes('m1', opts);
    expect(b).toBe(a);
    await flush();
    expect(Sock.all).toHaveLength(1);
    expect(Sock.all[0].url).toBe('ws://test/m1');
    Sock.all[0].hello();
    expect(a.status).toBe('live');

    releaseNotes('m1');
    expect(peekNotes('m1')).toBe(a);
    expect(Sock.all[0].closedWith).toBeNull();
    releaseNotes('m1');
    expect(peekNotes('m1')).toBeUndefined();
    expect(Sock.all[0].closedWith).toBe(1000);
  });

  it('meetings are independent', async () => {
    const a = acquireNotes('m1', opts);
    const b = acquireNotes('m2', opts);
    expect(a).not.toBe(b);
    await flush();
    expect(Sock.all.map((s) => s.url).sort()).toEqual(['ws://test/m1', 'ws://test/m2']);
    releaseNotes('m1');
    expect(Sock.all.find((s) => s.url.endsWith('m1'))!.closedWith).toBe(1000);
    expect(Sock.all.find((s) => s.url.endsWith('m2'))!.closedWith).toBeNull();
  });

  it('an acquire after the last release makes a fresh state; extra releases are harmless', async () => {
    const a = acquireNotes('m1', opts);
    releaseNotes('m1');
    releaseNotes('m1');
    const b = acquireNotes('m1', opts);
    expect(b).not.toBe(a);
    await flush();
    expect(Sock.all).toHaveLength(1); // the first never connected: it was released before the wasm promise
    expect(b.status).toBe('connecting');
  });

  it('listens to the bus only while something is open', async () => {
    const before = handlers();
    acquireNotes('m1', opts);
    acquireNotes('m2', opts);
    expect(handlers()).toBe(before + 1);
    releaseNotes('m1');
    expect(handlers()).toBe(before + 1);
    releaseNotes('m2');
    expect(handlers()).toBe(before);
  });
});

describe('the notes bus event', () => {
  it('reconnects a meeting that was waiting for its document, and only that one', async () => {
    const a = acquireNotes('m1', opts);
    const b = acquireNotes('m2', opts);
    await flush();
    Sock.all[0].hello({ read_only: true, waiting_for_sync: true });
    Sock.all[1].hello();
    expect(a.waitingForSync).toBe(true);
    expect(b.waitingForSync).toBe(false);
    bus({ type: 'notes', session_id: 'm2' }); // not waiting: left alone
    bus({ type: 'notes', session_id: 'other' });
    expect(Sock.all).toHaveLength(2);
    bus({ type: 'notes', session_id: 'm1' });
    expect(Sock.all).toHaveLength(3);
    expect(Sock.all[0].closedWith).toBe(1000);
    Sock.all[2].hello({ read_only: false });
    expect(a.waitingForSync).toBe(false);
    expect(a.readOnly).toBe(false);
    bus({ type: 'notes', session_id: 'm1' }); // live now: no more reconnects
    expect(Sock.all).toHaveLength(3);
  });
});
