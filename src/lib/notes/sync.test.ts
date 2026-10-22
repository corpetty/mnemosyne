import { describe, expect, it } from 'vitest';
import { ORIGIN, Relay, Client, converged, pair, sameBytes, WasmHashWeb } from './test-harness.js';
import { insertText, setLink, setLineKind } from './commands.js';
import { ensureTerminated, lines, openDoc } from './model.js';
import { ARTIFACT_TAG, DELTA_TAG, applyRemote, drain, openFromHello } from './sync.js';

describe('drain', () => {
  it('sends nothing for an untouched store and artifacts before the delta', () => {
    const web = new WasmHashWeb();
    const doc = openDoc(web, ORIGIN);
    expect(drain(web)).toEqual([]);
    ensureTerminated(doc);
    insertText(doc, 0, 'see');
    setLink(doc, 0, 3, 'https://example.com/long/enough/to/be/an/artifact');
    const frames = drain(web);
    expect(frames.map((f) => f[0])).toEqual([ARTIFACT_TAG, DELTA_TAG]);
    expect(drain(web)).toEqual([]);
  });
});

describe('applyRemote', () => {
  it('drains our pending ops before applying and never re-ships what it received', () => {
    const { relay, A, B } = pair();
    ensureTerminated(A.doc);
    insertText(A.doc, 0, 'from A');
    const [deltaA] = drain(A.web);
    insertText(B.doc, 0, 'from B');
    const { send, delivered } = applyRemote(B.web, deltaA);
    expect(delivered).toBeGreaterThan(0);
    expect(send.length).toBe(1);
    expect(drain(B.web)).toEqual([]); // A's delta is not sent back
    for (const f of send) relay.send(B, f);
    relay.settle();
    expect(converged(A, B)).toBe(true);
    expect(A.useless + B.useless).toBe(0);
  });

  it('an echoed delta delivers nothing and an unknown tag throws', () => {
    const web = new WasmHashWeb();
    const doc = openDoc(web, ORIGIN);
    ensureTerminated(doc);
    const [delta] = drain(web);
    expect(applyRemote(web, delta).delivered).toBe(0);
    expect(() => applyRemote(web, new Uint8Array([0x01, 2, 3]))).toThrow(/tag/);
    expect(applyRemote(web, new Uint8Array()).delivered).toBe(0);
  });

  it('artifact bytes are stored once', () => {
    const web = new WasmHashWeb();
    const doc = openDoc(web, ORIGIN);
    ensureTerminated(doc);
    insertText(doc, 0, 'x');
    setLink(doc, 0, 1, 'https://example.com/long/enough/to/be/an/artifact');
    const [art] = drain(web);
    const other = new WasmHashWeb();
    expect(applyRemote(other, art).delivered).toBe(1);
    expect(applyRemote(other, art).delivered).toBe(0);
  });
});

describe('openFromHello', () => {
  it('a snapshot joiner ships nothing back and matches the others', () => {
    const { relay, A, B } = pair();
    ensureTerminated(A.doc);
    insertText(A.doc, 0, 'x'.repeat(500));
    setLineKind(A.doc, 0, 0, 'h1');
    setLink(A.doc, 0, 5, 'https://example.com/long/enough/to/be/an/artifact');
    relay.settle();
    const snapshot = A.web.encode();
    const foldAt = relay.log.length;
    insertText(A.doc, 0, 'after fold ');
    insertText(B.doc, 11, ' B');
    relay.settle();
    const unfolded = relay.log.slice(foldAt);
    expect(unfolded.length).toBeGreaterThan(0);
    const web = openFromHello(snapshot, unfolded);
    expect(drain(web)).toEqual([]);
    const C = new Client('C', relay, web);
    expect(C.text()).toBe(A.text());
    expect(converged(A, B, C)).toBe(true);
    insertText(C.doc, 0, 'C: ');
    relay.settle();
    expect(converged(A, B, C)).toBe(true);
    expect(lines(A.doc)[0].kind).toBe('h1');
    expect(A.useless + B.useless + C.useless).toBe(0);
  });

  it('with no snapshot replays the frames, and with nothing gives an empty store', () => {
    const relay = new Relay();
    const A = new Client('A', relay);
    ensureTerminated(A.doc);
    insertText(A.doc, 0, 'hi');
    A.flush();
    const web = openFromHello(null, relay.log);
    expect(web.text(A.doc.obj)).toBe('hi\n');
    expect(drain(web)).toEqual([]);
    expect(sameBytes(web.encode(), A.web.encode())).toBe(true);
    const empty = openFromHello(new Uint8Array());
    expect(empty.objectCount()).toBe(0);
  });
});
