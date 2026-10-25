// A stand-in for guest/pkg/guest_wasm.js in tests (iroh.test.ts, transports.test.ts): connect()
// gives a FakeConn whose far end, the owner's sidecar, is played by the test.
import { FRAME_BINARY, FRAME_TEXT, type GuestConn, type GuestWasmLoader, type GuestWasmModule } from './iroh';

type Frame = { tag: number; data: Uint8Array };

/** A GuestConn whose far end is the test. */
export class FakeConn implements GuestConn {
  sent: Frame[] = [];
  freed = false;
  closedWith: { code: number; reason: string } | null = null;
  private inbox: (Frame | undefined | Error)[] = [];
  private waiter: ((f: Frame | undefined | Error) => void) | null = null;
  private resolveClosed!: (v: { code: number; reason: string }) => void;
  private closedP = new Promise<{ code: number; reason: string }>((r) => (this.resolveClosed = r));
  private ended = false;

  send(tag: number, bytes: Uint8Array): Promise<void> {
    if (this.closedWith) return Promise.reject(new Error('connection closed'));
    this.sent.push({ tag, data: bytes.slice() });
    return Promise.resolve();
  }
  recv(): Promise<Frame | undefined> {
    if (this.waiter) return Promise.reject(new Error('recv() is already pending'));
    const next = this.inbox.shift();
    if (next !== undefined || this.ended) return next instanceof Error ? Promise.reject(next) : Promise.resolve(next);
    return new Promise((res, rej) => {
      this.waiter = (f) => {
        this.waiter = null;
        if (f instanceof Error) rej(f);
        else res(f);
      };
    });
  }
  close(code: number, reason: string): Promise<void> {
    if (!this.closedWith) this.peerClose(code, reason);
    return Promise.resolve();
  }
  closed(): Promise<{ code: number; reason: string }> {
    return this.closedP;
  }
  free(): void {
    this.freed = true;
  }
  // the owner's side
  push(f: Frame | Error): void {
    if (this.waiter) this.waiter(f);
    else this.inbox.push(f);
  }
  text(s: string): void {
    this.push({ tag: FRAME_TEXT, data: new TextEncoder().encode(s) });
  }
  bin(b: Uint8Array): void {
    this.push({ tag: FRAME_BINARY, data: b });
  }
  peerClose(code: number, reason: string): void {
    this.closedWith = { code, reason };
    this.ended = true;
    if (this.waiter) this.waiter(undefined);
    this.resolveClosed({ code, reason });
  }
  get texts(): string[] {
    return this.sent.filter((f) => f.tag === FRAME_TEXT).map((f) => new TextDecoder().decode(f.data));
  }
}

/** A guest_wasm stand-in: connect() resolves to a FakeConn, or as `outcome` says. */
export function fakeWasm(): {
  loader: GuestWasmLoader;
  conns: FakeConn[];
  calls: string[][];
  inits: number;
  outcome: 'ok' | 'reject' | 'hang';
  pending: (() => void)[];
} {
  const state = {
    conns: [] as FakeConn[],
    calls: [] as string[][],
    inits: 0,
    outcome: 'ok' as 'ok' | 'reject' | 'hang',
    pending: [] as (() => void)[],
    loader: null as unknown as GuestWasmLoader
  };
  const mod: GuestWasmModule = {
    default: async () => {
      state.inits++;
    },
    connect: (endpoint, relay, alpn) => {
      state.calls.push([endpoint, relay, alpn]);
      if (state.outcome === 'reject') return Promise.reject(new Error('no route to endpoint'));
      const conn = new FakeConn();
      if (state.outcome === 'hang')
        return new Promise((res) => {
          state.pending.push(() => {
            state.conns.push(conn);
            res(conn);
          });
        });
      state.conns.push(conn);
      return Promise.resolve(conn);
    }
  };
  state.loader = () => Promise.resolve(mod);
  return state;
}
