// One place that initialises hashseq's wasm bindings (src/lib/notes/pkg/, built by
// scripts/build-notes-wasm.sh). In the browser the module is fetched by URL; in Node and
// Vitest the caller hands over the file bytes (`initWasmSync`). Both are idempotent: a second
// call is a no-op, and the browser path dedupes concurrent callers on one promise.
import init, { initSync, WasmHashWeb } from './pkg/notes_wasm.js';

export { WasmHashWeb };

let done = false;
let pending: Promise<void> | null = null;

/** Load and instantiate the module over the network (the app's path). */
export function ensureWasm(): Promise<void> {
  if (done) return Promise.resolve();
  if (!pending) {
    pending = (async () => {
      const url = (await import('./pkg/notes_wasm_bg.wasm?url')).default;
      await init({ module_or_path: new URL(url, globalThis.location?.href ?? 'http://localhost/') });
      done = true;
    })().catch((e) => {
      pending = null;
      throw e;
    });
  }
  return pending;
}

/** Instantiate from the module's bytes, synchronously (Node, Vitest). */
export function initWasmSync(bytes: BufferSource | WebAssembly.Module): void {
  if (done) return;
  initSync({ module: bytes });
  done = true;
}

export function wasmReady(): boolean {
  return done;
}
