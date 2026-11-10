import { describe, expect, it } from 'vitest';
import { CODE_LENGTHS, codeLength, until } from './codeLength.svelte.js';

describe('how long a pairing code works', () => {
  it('defaults to an hour and remembers a choice', () => {
    expect(codeLength.minutes).toBe(60);
    codeLength.minutes = 24 * 60;
    expect(localStorage.getItem('mnemosyne.codeLength')).toBe('1440');
    codeLength.minutes = 60;
  });

  it('offers only lengths the backend takes (5 to 1440 minutes)', () => {
    for (const l of CODE_LENGTHS) expect(l.minutes >= 5 && l.minutes <= 1440).toBe(true);
  });

  it('names the day only when the code ends on another day', () => {
    const now = new Date(2026, 10, 10, 9, 0);
    const today = until(new Date(2026, 10, 10, 10, 0).toISOString(), now);
    const tomorrow = until(new Date(2026, 10, 11, 9, 0).toISOString(), now);
    expect(today).not.toMatch(/Nov|11/);
    expect(tomorrow).toContain('11');
    expect(tomorrow.length).toBeGreaterThan(today.length);
  });
});
