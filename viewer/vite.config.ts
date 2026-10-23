// The publication viewer (phase 7): a standalone page, not a SvelteKit route. Built by
// scripts/build-viewer.sh into backend/mnemosyne/api/static/viewer/ (committed; CI rebuilds and
// compares). index.html refers to its assets as /p/assets/<name>-<hash>.{js,css}, so the same
// files work at /p/<id> on the backend and on the publish host, which both serve /p/assets/*.
import { svelte } from '@sveltejs/vite-plugin-svelte';
import tailwindcss from '@tailwindcss/vite';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import { singleFile } from './single';

const here = (p: string) => fileURLToPath(new URL(p, import.meta.url));

export default defineConfig({
	root: here('.'),
	base: '/p/',
	publicDir: false,
	envDir: false,
	clearScreen: false,
	plugins: [tailwindcss(), svelte({ configFile: false }), singleFile()],
	resolve: {
		// Markdown.svelte (shared with the app) imports Tauri lazily to open links.
		alias: { '@tauri-apps/api/core': here('./src/no-tauri.ts') }
	},
	build: {
		outDir: here('../backend/mnemosyne/api/static/viewer'),
		emptyOutDir: true,
		// One JS file (single.html inlines it) and no polyfill or preload tags.
		modulePreload: false,
		assetsInlineLimit: 0,
		reportCompressedSize: false,
		rollupOptions: { output: { inlineDynamicImports: true } }
	}
});
