/** Open a link in the system browser (Tauri's shell plugin), or a new tab in a browser. */
export async function openLink(href: string): Promise<void> {
	try {
		const { invoke } = await import('@tauri-apps/api/core');
		await invoke('plugin:shell|open', { path: href });
	} catch {
		window.open(href, '_blank', 'noopener,noreferrer');
	}
}

/** Click handler for an `<a href>`: keeps the href for middle-click, opens it the app's way. */
export function onExternalClick(e: MouseEvent & { currentTarget: HTMLAnchorElement }): void {
	if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey) return;
	e.preventDefault();
	void openLink(e.currentTarget.href);
}
