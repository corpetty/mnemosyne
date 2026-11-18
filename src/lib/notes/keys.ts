// Keyboard platform helpers, shared by the app, the notes editor and the guest page (so they live
// here, where src/lib/notes/ui may import them). Pure: nothing is read before it is called, so a
// test can stub `navigator` per case.
//
// "Primary" is the shortcut modifier: Cmd on a Mac, Ctrl elsewhere. On a Mac a Ctrl-only press does
// not count: Ctrl+A / E / K / B / F ... move the caret in every macOS text field.

/**
 * True on macOS (also iPadOS asking for a desktop site). False without a `navigator`.
 * The platform decides: user agents say "Macintosh" elsewhere too (WebKitGTK and Playwright's
 * WebKit on Linux send Safari's), so the user agent counts only when no platform is given.
 */
export function isMac(): boolean {
  if (typeof navigator === 'undefined') return false;
  const platform = navigator.platform ?? '';
  if (platform) return /^Mac/.test(platform);
  return /Macintosh/.test(navigator.userAgent ?? '');
}

/** The shortcut modifier is down: metaKey on a Mac, ctrlKey elsewhere. */
export function primary(e: Pick<KeyboardEvent, 'ctrlKey' | 'metaKey'>): boolean {
  return isMac() ? e.metaKey : e.ctrlKey;
}

/** The modifier that is NOT primary (Ctrl on a Mac, Cmd/Super elsewhere), for exact matches. */
export function foreignMod(e: Pick<KeyboardEvent, 'ctrlKey' | 'metaKey'>): boolean {
  return isMac() ? e.ctrlKey : e.metaKey;
}

const MAC_KEYS: Record<string, string> = {
  Enter: '↩',
  Backspace: '⌫',
  Delete: '⌦',
  Escape: '⎋',
  Tab: '⇥'
};

/**
 * Shortcut text for a spec like `'Mod+Shift+M'`. Parts are joined by `+`; `Mod` is Cmd on a Mac
 * and Ctrl elsewhere, `Ctrl` is always a literal Ctrl, then `Alt`, `Shift` and the key.
 *   Linux/Windows: "Ctrl+Shift+M" (the parts as written, Mod as Ctrl)
 *   Mac:           "⇧⌘M" (Apple's order: ⌃ ⌥ ⇧ ⌘, then the key)
 */
export function keyHint(spec: string): string {
  const parts = spec.split('+');
  // A trailing "+" key ("Mod++") splits into an empty last part.
  const key = parts.pop() || '+';
  if (!isMac()) {
    return [...parts.map((p) => (p === 'Mod' ? 'Ctrl' : p)), key].join('+');
  }
  const has = (m: string) => parts.includes(m);
  const glyphs = (has('Ctrl') ? '⌃' : '') + (has('Alt') ? '⌥' : '') + (has('Shift') ? '⇧' : '') + (has('Mod') ? '⌘' : '');
  return glyphs + (MAC_KEYS[key] ?? key.toUpperCase());
}
