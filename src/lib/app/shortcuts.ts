// The app's own keyboard shortcuts (controller.svelte.ts handleKeydown) and their hint text.
// Cmd on a Mac, Ctrl elsewhere (src/lib/notes/keys.ts). Never bind the system's own: Cmd+Q, W, H,
// M (Minimize), comma.
import { isMac, keyHint, primary } from '$lib/notes/keys.js';

/** Specs for keyHint(). Mark this moment: Ctrl+M on Linux; Cmd+M is Minimize on a Mac. */
export const shortcutSpec = {
  palette: 'Mod+K',
  record: 'Mod+R',
  stop: 'Mod+S',
  export: 'Mod+E',
  sidebar: 'Mod+B',
  mark: () => (isMac() ? 'Mod+Shift+M' : 'Mod+M')
};

type Name = keyof typeof shortcutSpec;

/** The text to show for a shortcut ("Ctrl+S", "⌘S"). */
export function shortcutHint(name: Name): string {
  const s = shortcutSpec[name];
  return keyHint(typeof s === 'function' ? s() : s);
}

/**
 * The letter of the app shortcut this key press is, or null. Linux/Windows: Ctrl held, the key as
 * the browser reports it (Shift does not stop a match, as before). Mac: Cmd held without Alt;
 * Shift only for Mark (Cmd+Shift+M), where it must be present.
 */
export function appShortcut(e: KeyboardEvent): string | null {
  if (!primary(e)) return null;
  if (!isMac()) return e.key;
  if (e.altKey) return null;
  const k = e.key.toLowerCase();
  // Mark needs Shift (plain Cmd+M is Minimize); the others must not have it.
  return (k === 'm') === e.shiftKey ? k : null;
}
