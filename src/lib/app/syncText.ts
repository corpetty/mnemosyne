import type { SyncPeerStatus } from '$lib/types/index.js';

/** "just now", "2 min ago", "3 h ago", "5 d ago". */
export function ago(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return 'never';
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (s < 45) return 'just now';
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

/** " · 1 meeting waiting for its notes to merge", or nothing: meetings left for the next round. */
export function waitingNote(p: Pick<SyncPeerStatus, 'waiting'>): string {
  const n = p.waiting ?? 0;
  return n > 0 ? ` · ${n} meeting${n === 1 ? '' : 's'} waiting for ${n === 1 ? 'its' : 'their'} notes to merge` : '';
}

/** One line for a sync row: what the round with that computer came to. */
export function peerState(p: SyncPeerStatus, now = Date.now()): string {
  switch (p.state) {
    case 'syncing':
      return p.total ? `Syncing… ${p.done} of ${p.total}` : 'Syncing…';
    case 'done':
      return `Synced ${ago(p.last_sync_at, now)}${waitingNote(p)}`;
    case 'offline':
      return p.last_sync_at ? `Offline since ${clock(p.last_sync_at)}` : 'Offline';
    case 'locked':
      return 'Locked';
    case 'skew':
      return 'Needs an update';
    case 'error':
      return p.error || 'Sync failed';
    default:
      return p.last_sync_at ? `Synced ${ago(p.last_sync_at, now)}` : 'Waiting';
  }
}
