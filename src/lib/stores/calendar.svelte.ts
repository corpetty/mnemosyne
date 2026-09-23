import type { CalendarEvent } from '$lib/types/index.js';
import * as api from '$lib/api/backend.js';

const DISMISSED_KEY = 'mnemosyne.dismissedEvents';

/** Upcoming meetings from the backend's ICS feed, refreshed every minute. */
class CalendarState {
  configured = $state(false);
  error = $state<string | null>(null);
  current = $state<CalendarEvent | null>(null);
  upcoming = $state<CalendarEvent[]>([]);
  now = $state(Date.now());
  private dismissed = $state<string[]>([]);
  private timer: ReturnType<typeof setInterval> | null = null;

  constructor() {
    try {
      this.dismissed = JSON.parse(localStorage.getItem(DISMISSED_KEY) ?? '[]');
    } catch {
      this.dismissed = [];
    }
  }

  start() {
    this.stop();
    this.refresh();
    this.timer = setInterval(() => {
      this.now = Date.now();
      this.refresh();
    }, 60_000);
  }

  stop() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  async refresh(force = false) {
    try {
      const r = await api.getCalendar(12, force);
      this.configured = r.configured;
      this.error = r.error;
      this.current = r.current;
      this.upcoming = r.upcoming;
      this.now = Date.now();
    } catch {
      /* backend unreachable; keep last state */
    }
  }

  /** A meeting that started in the last 3 minutes or starts in the next minute. */
  get starting(): CalendarEvent | null {
    const now = this.now;
    return (
      this.upcoming.find((e) => {
        const start = new Date(e.start).getTime();
        return start - now <= 60_000 && now - start <= 180_000 && !this.dismissed.includes(e.uid);
      }) ?? null
    );
  }

  dismiss(uid: string) {
    this.dismissed = [...this.dismissed.slice(-50), uid];
    try {
      localStorage.setItem(DISMISSED_KEY, JSON.stringify(this.dismissed));
    } catch {
      /* ignore */
    }
  }
}

export const calendarState = new CalendarState();

export function fmtClock(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

export function fmtRelative(iso: string, now = Date.now()): string {
  const mins = Math.round((new Date(iso).getTime() - now) / 60_000);
  if (mins <= 0) return 'now';
  if (mins < 60) return `in ${mins} min`;
  const h = Math.floor(mins / 60);
  return `in ${h} h ${mins % 60 ? `${mins % 60} min` : ''}`.trim();
}
