// How long a new pairing code or invite works (phone, another computer, sync, a contact). One
// choice for all of them, remembered in this browser; the backend's default is an hour and it
// takes 5 to 1440 minutes (services/pairing.py CODE_MINUTES_MIN/MAX).

export const CODE_LENGTHS = [
  { minutes: 15, label: '15 minutes' },
  { minutes: 60, label: '1 hour' },
  { minutes: 24 * 60, label: '1 day' }
] as const;

const KEY = 'mnemosyne.codeLength';
const DEFAULT = 60;

function load(): number {
  try {
    const n = Number(localStorage.getItem(KEY));
    return CODE_LENGTHS.some((l) => l.minutes === n) ? n : DEFAULT;
  } catch {
    return DEFAULT;
  }
}

class CodeLength {
  #minutes = $state(load());

  get minutes(): number {
    return this.#minutes;
  }

  set minutes(n: number) {
    this.#minutes = n;
    try {
      localStorage.setItem(KEY, String(n));
    } catch {
      // A private window: the choice lasts this page only.
    }
  }
}

export const codeLength = new CodeLength();

/** When a code stops working: the time, with the day when that is not today. */
export function until(iso: string, now = new Date()): string {
  const at = new Date(iso);
  const time = at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (at.toDateString() === now.toDateString()) return time;
  return `${at.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })} ${time}`;
}
