import { afterEach, describe, expect, it } from 'vitest';
import { pinPlatform, restorePlatform } from '$lib/notes/test-platform.js';
import { appShortcut, shortcutHint } from './shortcuts.js';

afterEach(restorePlatform);

const ev = (key: string, init: KeyboardEventInit = {}) => new KeyboardEvent('keydown', { key, ...init });

describe('appShortcut on Linux', () => {
  it('needs Ctrl, and Shift or Cmd alone do not do', () => {
    pinPlatform('linux');
    expect(appShortcut(ev('k', { ctrlKey: true }))).toBe('k');
    expect(appShortcut(ev('m', { ctrlKey: true }))).toBe('m');
    expect(appShortcut(ev('k'))).toBeNull();
    expect(appShortcut(ev('k', { metaKey: true }))).toBeNull();
  });

  it('keeps today\'s reading of the key: Shift does not stop a match, only the key as typed counts', () => {
    pinPlatform('linux');
    expect(appShortcut(ev('s', { ctrlKey: true, shiftKey: true }))).toBe('s');
    expect(appShortcut(ev('S', { ctrlKey: true, shiftKey: true }))).toBe('S');
  });
});

describe('appShortcut on a Mac', () => {
  it('needs Cmd; a Ctrl-only press is a text-editing key', () => {
    pinPlatform('mac');
    for (const k of ['k', 'r', 's', 'e', 'b']) {
      expect(appShortcut(ev(k, { metaKey: true }))).toBe(k);
      expect(appShortcut(ev(k, { ctrlKey: true }))).toBeNull();
    }
  });

  it('marks a moment with Cmd+Shift+M, never plain Cmd+M (Minimize)', () => {
    pinPlatform('mac');
    expect(appShortcut(ev('M', { metaKey: true, shiftKey: true }))).toBe('m');
    expect(appShortcut(ev('m', { metaKey: true }))).toBeNull();
    expect(appShortcut(ev('m', { ctrlKey: true }))).toBeNull();
  });

  it('leaves Shift and Option variants of the others alone', () => {
    pinPlatform('mac');
    expect(appShortcut(ev('K', { metaKey: true, shiftKey: true }))).toBeNull();
    expect(appShortcut(ev('s', { metaKey: true, altKey: true }))).toBeNull();
  });
});

describe('shortcutHint', () => {
  it('prints the text the UI has always had on Linux', () => {
    pinPlatform('linux');
    expect(shortcutHint('palette')).toBe('Ctrl+K');
    expect(shortcutHint('record')).toBe('Ctrl+R');
    expect(shortcutHint('stop')).toBe('Ctrl+S');
    expect(shortcutHint('export')).toBe('Ctrl+E');
    expect(shortcutHint('sidebar')).toBe('Ctrl+B');
    expect(shortcutHint('mark')).toBe('Ctrl+M');
  });

  it('prints Mac glyphs on a Mac', () => {
    pinPlatform('mac');
    expect(shortcutHint('palette')).toBe('⌘K');
    expect(shortcutHint('stop')).toBe('⌘S');
    expect(shortcutHint('mark')).toBe('⇧⌘M');
  });
});
