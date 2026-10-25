// The guest notes page (phase 9): a standalone page, not a SvelteKit route. Built by
// scripts/build-guest.sh into backend/mnemosyne/api/static/guest/ (committed; CI rebuilds and
// compares). index.html refers to its assets relatively (./assets/...), so the same files work
// at /n/<room>, /n/ and /mnemosyne/n/ (GitHub Pages); the servers serve /n/assets/*.
import { svelte } from '@sveltejs/vite-plugin-svelte';
import tailwindcss from '@tailwindcss/vite';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';

const here = (p: string) => fileURLToPath(new URL(p, import.meta.url));

export default defineConfig({
	root: here('.'),
	base: './',
	publicDir: false,
	envDir: false,
	clearScreen: false,
	plugins: [tailwindcss(), svelte({ configFile: false })],
	resolve: {
		// The notes model, editor and wasm loader live in the app's src/lib.
		alias: { $lib: here('../src/lib') }
	},
	build: {
		outDir: here('../backend/mnemosyne/api/static/guest'),
		emptyOutDir: true,
		target: 'esnext',
		// No polyfill or preload tags; the wasm is fetched as its own file (assetsInlineLimit 0).
		modulePreload: false,
		assetsInlineLimit: 0,
		reportCompressedSize: false,
		// Code splitting stays on: the iroh client (guest/pkg glue + its wasm) is a lazily loaded chunk.
		rollupOptions: { output: { entryFileNames: 'assets/[name]-[hash].js', chunkFileNames: 'assets/[name]-[hash].js', assetFileNames: 'assets/[name]-[hash][extname]' } }
	}
});
