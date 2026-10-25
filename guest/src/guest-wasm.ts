// The browser iroh client, guest/pkg/guest_wasm.js (guest-wasm/, built by CI and committed), as a
// lazy chunk: only imported once a format 2 link is opened. Kept apart from iroh.ts so tests,
// which give IrohSocket a fake module, never load the wasm. Its generated types say `any` for
// the promises; iroh.ts's GuestWasmModule narrows them.
import type { GuestWasmLoader } from './iroh';

export const loadGuestWasm: GuestWasmLoader = () => import('../pkg/guest_wasm.js');
