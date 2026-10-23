// Stands in for @tauri-apps/api/core in the viewer build (vite.config.ts alias). Markdown.svelte
// opens links through Tauri's shell when it can; here it always falls back to window.open.
export function invoke(): Promise<never> {
	return Promise.reject(new Error('Not in the desktop app'));
}
