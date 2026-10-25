// Which ways the guest page tries to reach its room, in order (phase 10, C7): iroh to the owner's
// computer when the link names an endpoint and a relay, then the WebSocket of the host serving
// the page, only when the path carries `/n/<room>` (a publish host, the backend or a tunnel; a
// static copy such as GitHub Pages has no room behind it). RoomClient (room.ts) does the trying:
// iroh gets IROH_BUDGET_MS after the wasm is up, then the host; whichever welcomes us first is kept.
import { guestWasm, irohSocketCtor, irohUrl, type GuestWasmLoader } from './iroh';
import { relayUrl, type GuestLink } from './link';
import { roomUrl, type RoomSocketCtor, type RoomTransport } from './room';

/** How long iroh may take to welcome us, counted from the wasm being ready, before the host is tried (ms). */
export const IROH_BUDGET_MS = 8000;

export interface TransportOptions {
  /** The page's origin (for the host's socket URL). */
  loc?: { protocol: string; host: string };
  /** The host's WebSocket (tests). */
  webSocket?: RoomSocketCtor;
  /** The iroh wasm module: guest-wasm.ts `loadGuestWasm` on the page, a fake in tests. */
  loader: GuestWasmLoader;
  /** Asked after the host's socket failed before it opened. */
  probe?: () => Promise<'gone' | 'full' | null>;
  budgetMs?: number;
}

export function transportsFor(link: GuestLink, opts: TransportOptions): RoomTransport[] {
  const out: RoomTransport[] = [];
  if (link.endpoint && link.relay) {
    const loader = opts.loader;
    out.push({
      kind: 'iroh',
      url: irohUrl(link.endpoint, relayUrl(link.relay)),
      webSocket: irohSocketCtor({ loader }),
      prepare: () => guestWasm(loader),
      budgetMs: opts.budgetMs ?? IROH_BUDGET_MS
    });
  }
  if (link.hostPath) {
    out.push({
      kind: 'host',
      url: roomUrl(link.room, opts.loc ?? location),
      webSocket: opts.webSocket,
      probe: opts.probe
    });
  }
  return out;
}
