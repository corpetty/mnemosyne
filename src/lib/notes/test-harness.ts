// Shared test fixtures: the wasm loaded from file bytes, a fake relay that forwards frames to
// every other client and never echoes (like /ws/notes/{id}), and compact renderings of a
// document for assertions. Ported from notes-wasm/tests/converge.mjs.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { initWasmSync, WasmHashWeb } from './wasm.js';
import { lines, openDoc, type Doc, type Line } from './model.js';
import { applyRemote, drain } from './sync.js';

// Under jsdom import.meta.url is an http URL; Vitest runs from the repo root.
initWasmSync(readFileSync(resolve(process.cwd(), 'src/lib/notes/pkg/notes_wasm_bg.wasm')));

export const ORIGIN = 'a1'.repeat(32);

export class Relay {
  clients: Client[] = [];
  queues = new Map<Client, Uint8Array[]>();
  log: Uint8Array[] = [];
  artifactFrames = 0;

  join(client: Client): void {
    this.clients.push(client);
    this.queues.set(client, []);
  }

  send(from: Client, frame: Uint8Array): void {
    if (frame[0] !== 0xde && frame[0] !== 0xaf) throw new Error(`bad frame tag ${frame[0]}`);
    this.log.push(frame);
    if (frame[0] === 0xaf) this.artifactFrames++;
    for (const c of this.clients) if (c !== from) (this.queues.get(c) as Uint8Array[]).push(frame);
  }

  /** Deliver until quiet: every queue empty and nobody has anything left to send. */
  settle(): void {
    for (let round = 0; round < 100; round++) {
      let moved = false;
      for (const c of this.clients) {
        const q = this.queues.get(c) as Uint8Array[];
        while (q.length) {
          c.receive(q.shift() as Uint8Array);
          moved = true;
        }
        if (c.flush()) moved = true;
      }
      if (!moved) return;
    }
    throw new Error('relay did not settle');
  }
}

export class Client {
  web: WasmHashWeb;
  doc: Doc;
  useless = 0;

  constructor(
    public name: string,
    public relay: Relay,
    web?: WasmHashWeb
  ) {
    this.web = web ?? new WasmHashWeb();
    this.doc = openDoc(this.web, ORIGIN);
    relay.join(this);
  }

  flush(): boolean {
    const frames = drain(this.web);
    for (const f of frames) this.relay.send(this, f);
    return frames.length > 0;
  }

  receive(frame: Uint8Array): void {
    const { send, delivered } = applyRemote(this.web, frame);
    for (const f of send) this.relay.send(this, f);
    if (frame[0] === 0xde && delivered === 0) this.useless++;
  }

  text(): string {
    return this.web.text(this.doc.obj);
  }

  lines(): Line[] {
    return lines(this.doc);
  }

  /** `"text":kind{runs}` per line, for assertions. */
  view(): string {
    return view(this.lines());
  }
}

export function pair(): { relay: Relay; A: Client; B: Client } {
  const relay = new Relay();
  const A = new Client('A', relay);
  const B = new Client('B', relay);
  return { relay, A, B };
}

export function single(): Client {
  return new Client('A', new Relay());
}

export function sameBytes(a: Uint8Array, b: Uint8Array): boolean {
  return a.length === b.length && a.every((x, i) => x === b[i]);
}

/** Converged: byte-equal encode() and equal lines(). */
export function converged(...clients: Client[]): boolean {
  const enc = clients[0].web.encode();
  const v = clients[0].view();
  return clients.every((c) => sameBytes(c.web.encode(), enc) && c.view() === v);
}

/** One line of the compact rendering: text, kind, then marked runs if any. */
export function view(ls: Line[]): string {
  return ls
    .map((l) => {
      const runs = l.runs
        .filter((r) => r.bold || r.italic || r.strike || r.code || r.link || r.linkConflict || r.comments.length)
        .map((r) => {
          const f: string[] = [];
          if (r.bold) f.push('b');
          if (r.italic) f.push('i');
          if (r.strike) f.push('s');
          if (r.code) f.push('c');
          if (r.link) f.push(`link=${r.link}`);
          if (r.linkConflict) f.push(`link?${r.linkConflict.join('|')}`);
          for (const c of r.comments) f.push(`#${c}`);
          return `${JSON.stringify(r.text)}{${f.join(',')}}`;
        });
      const extra = [l.lang ? `lang=${l.lang}` : '', l.align ? `align=${l.align.map((a) => a ?? '-').join(',')}` : '']
        .filter(Boolean)
        .join(' ');
      const cells = l.cells ? ' cells=' + JSON.stringify(l.cells.map((c) => c.map((r) => r.text).join(''))) : '';
      return `${JSON.stringify(l.text)}:${l.kinds.join('|') || '-'}${extra ? ' ' + extra : ''}${cells}${runs.length ? ' ' + runs.join(' ') : ''}`;
    })
    .join('\n');
}

export { WasmHashWeb };
