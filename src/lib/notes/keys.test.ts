import { afterEach, describe, expect, it, vi } from 'vitest';
import { foreignMod, isMac, keyHint, primary } from './keys.js';
import { pinPlatform, restorePlatform } from './test-platform.js';

afterEach(restorePlatform);

describe('isMac', () => {
  it('is true for a Mac platform or a Macintosh user agent', () => {
    pinPlatform('mac');
    expect(isMac()).toBe(true);
    vi.stubGlobal('navigator', { platform: '', userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)' });
    expect(isMac()).toBe(true);
  });

  it('is false on Linux and Windows', () => {
    pinPlatform('linux');
    expect(isMac()).toBe(false);
    pinPlatform('windows');
    expect(isMac()).toBe(false);
  });

  it('is false without a navigator', () => {
    vi.stubGlobal('navigator', undefined);
    expect(isMac()).toBe(false);
    expect(keyHint('Mod+K')).toBe('Ctrl+K');
  });
});

describe('primary', () => {
  it('is Ctrl on Linux and Windows, and Cmd is not it', () => {
    for (const p of ['linux', 'windows'] as const) {
      pinPlatform(p);
      expect(primary({ ctrlKey: true, metaKey: false })).toBe(true);
      expect(primary({ ctrlKey: false, metaKey: true })).toBe(false);
      expect(primary({ ctrlKey: false, metaKey: false })).toBe(false);
      expect(foreignMod({ ctrlKey: false, metaKey: true })).toBe(true);
    }
  });

  it('is Cmd on a Mac, and a Ctrl-only press does not count', () => {
    pinPlatform('mac');
    expect(primary({ ctrlKey: false, metaKey: true })).toBe(true);
    expect(primary({ ctrlKey: true, metaKey: false })).toBe(false);
    expect(foreignMod({ ctrlKey: true, metaKey: false })).toBe(true);
  });
});

describe('keyHint', () => {
  it('spells the shortcuts as the app always has outside the Mac', () => {
    for (const p of ['linux', 'windows'] as const) {
      pinPlatform(p);
      expect(keyHint('Mod+K')).toBe('Ctrl+K');
      expect(keyHint('Mod+Shift+M')).toBe('Ctrl+Shift+M');
      expect(keyHint('Mod+Alt+P')).toBe('Ctrl+Alt+P');
      expect(keyHint('Mod+Shift+9')).toBe('Ctrl+Shift+9');
      expect(keyHint('Mod+/')).toBe('Ctrl+/');
      expect(keyHint('Mod+Enter')).toBe('Ctrl+Enter');
      expect(keyHint('Ctrl+M')).toBe('Ctrl+M');
    }
  });

  it('uses Mac glyphs in Apple order on a Mac', () => {
    pinPlatform('mac');
    expect(keyHint('Mod+K')).toBe('⌘K');
    expect(keyHint('Mod+Shift+M')).toBe('⇧⌘M');
    expect(keyHint('Mod+Alt+P')).toBe('⌥⌘P');
    expect(keyHint('Ctrl+Alt+Shift+Mod+X')).toBe('⌃⌥⇧⌘X');
    expect(keyHint('Ctrl+M')).toBe('⌃M');
    expect(keyHint('Mod+Shift+9')).toBe('⇧⌘9');
    expect(keyHint('Mod+/')).toBe('⌘/');
    expect(keyHint('Mod+Enter')).toBe('⌘↩');
  });
});
