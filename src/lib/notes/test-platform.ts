// For tests: pin the keyboard platform (src/lib/notes/keys.ts) so a case does not depend on the
// machine running it (a Mac CI runner included). Undo with `restorePlatform()` in afterEach.
import { vi } from 'vitest';

export type TestPlatform = 'linux' | 'windows' | 'mac';

const PLATFORMS: Record<TestPlatform, { platform: string; userAgent: string }> = {
  linux: { platform: 'Linux x86_64', userAgent: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 (KHTML, like Gecko)' },
  windows: { platform: 'Win32', userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' },
  mac: { platform: 'MacIntel', userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15' }
};

let spies: { mockRestore: () => void }[] = [];

export function pinPlatform(p: TestPlatform): void {
  restorePlatform();
  spies = [
    vi.spyOn(window.navigator, 'platform', 'get').mockReturnValue(PLATFORMS[p].platform),
    vi.spyOn(window.navigator, 'userAgent', 'get').mockReturnValue(PLATFORMS[p].userAgent)
  ];
}

/** Undo pinPlatform (and a `vi.stubGlobal('navigator', ...)` in a test). */
export function restorePlatform(): void {
  for (const s of spies) s.mockRestore();
  spies = [];
  vi.unstubAllGlobals();
}
