// Unit tests for the pure editor parts (src/lib/notes). Kept apart from vite.config.ts so
// `pnpm build` and `pnpm check` stay untouched; the wasm is loaded from file bytes by the
// tests' harness, not fetched.
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  // Compiles runes in *.svelte.ts stores (notes/doc.svelte.ts).
  plugins: [svelte()],
  // SvelteKit's $lib, for stores that import through it.
  resolve: {
    alias: { $lib: fileURLToPath(new URL('./src/lib', import.meta.url)) },
    // Svelte's client build, so component tests (viewer/src/Meeting.test.ts) can mount().
    conditions: ['browser']
  },
  test: {
    environment: 'jsdom',
    include: [
      'src/lib/notes/**/*.test.ts',
      'src/lib/stores/**/*.test.ts',
      'viewer/src/**/*.test.ts',
      'guest/src/**/*.test.ts'
    ]
  }
});
