import { getSyncStatus } from '$lib/api/backend.js';
import type { SyncPeerStatus, SyncStatus } from '$lib/types/index.js';

/** Library sync with the person's other computers (backend services/sync_service.py). */
class SyncState {
  status = $state<SyncStatus | null>(null);
  private loading: Promise<void> | null = null;

  get peers(): SyncPeerStatus[] {
    return this.status?.peers ?? [];
  }

  /** The computer's name for a meeting's `origin` ('' = recorded here). */
  name(originId: string): string {
    if (!originId) return this.status?.self.name || 'this computer';
    return this.peers.find((p) => p.id === originId)?.name || 'another computer';
  }

  /** Why a foreign meeting's audio cannot be used here. */
  audioOn(originId: string): string {
    return `The audio is on ${this.name(originId)}`;
  }

  async load(): Promise<void> {
    if (this.loading) return this.loading;
    this.loading = (async () => {
      try {
        this.status = await getSyncStatus();
      } catch {
        this.status = null; // sync off, a team server or an older backend
      } finally {
        this.loading = null;
      }
    })();
    return this.loading;
  }

  /** A `sync` event: keep the row's state without a refetch, and refetch when it ended. */
  apply(peer: string, state: SyncPeerStatus['state'], done: number, total: number): boolean {
    const row = this.peers.find((p) => p.id === peer);
    if (!row) {
      void this.load();
      return false;
    }
    row.state = state;
    row.done = done;
    row.total = total;
    if (state !== 'syncing') void this.load();
    return state !== 'syncing';
  }
}

export const syncState = new SyncState();
