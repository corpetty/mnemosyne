import {
  getContacts,
  getPeerShares,
  getShareStatus,
  putPeerShare,
  removePeerShare
} from '$lib/api/backend.js';
import type {
  ContactInfo,
  ContactsMe,
  PeerShareInfo,
  ShareParts,
  ShareStatus
} from '$lib/types/index.js';

/** People this person shares meetings with (backend services/contacts.py, share_pull.py). */
class ContactsState {
  me = $state<ContactsMe | null>(null);
  contacts = $state<ContactInfo[]>([]);
  loaded = $state(false);
  /** False on a team server (the backend answers 409) or an older backend. */
  available = $state(true);
  error = $state('');
  /** Live shares per meeting I own. */
  peerShares = $state<Record<string, PeerShareInfo[]>>({});
  /** How my work on a meeting shared with me stands; null = not shared with me. */
  shareStatus = $state<Record<string, ShareStatus | null>>({});
  private loading: Promise<void> | null = null;

  async load(): Promise<void> {
    if (this.loading) return this.loading;
    this.loading = (async () => {
      try {
        const r = await getContacts();
        this.me = r.me;
        this.contacts = r.contacts;
        this.available = true;
        this.error = '';
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        if (msg.startsWith('409') || msg.startsWith('404')) this.available = false;
        else this.error = msg;
      } finally {
        this.loaded = true;
        this.loading = null;
      }
    })();
    return this.loading;
  }

  byPerson(person: string): ContactInfo | undefined {
    return this.contacts.find((c) => c.person === person);
  }

  /** What to call someone: the name I gave them, else the one on their card. */
  name(person: string): string {
    if (this.me && person === this.me.person) return this.me.name || 'Me';
    return this.byPerson(person)?.name || 'Someone';
  }

  /** `pid` is the short id comments and authors carry. */
  isMe(pid: string): boolean {
    return !!pid && this.me?.pid === pid;
  }

  /** A `share_pull` event: keep the contact's row in step without a refetch. */
  applyPull(event: { person: string; state: string; error: string | null }): void {
    const c = this.byPerson(event.person);
    if (!c) {
      void this.load(); // a contact this computer has not heard of yet
      return;
    }
    c.pull = {
      state: event.state,
      at: event.state === 'pulling' ? (c.pull?.at ?? null) : new Date().toISOString(),
      error: event.error
    };
  }

  async loadShares(sessionId: string): Promise<void> {
    try {
      this.peerShares[sessionId] = await getPeerShares(sessionId);
    } catch {
      this.peerShares[sessionId] = [];
    }
  }

  async share(sessionId: string, person: string, parts: ShareParts): Promise<void> {
    const info = await putPeerShare(sessionId, person, parts);
    const rest = (this.peerShares[sessionId] ?? []).filter((s) => s.person !== person);
    this.peerShares[sessionId] = [...rest, info];
  }

  async unshare(sessionId: string, person: string): Promise<void> {
    await removePeerShare(sessionId, person);
    this.peerShares[sessionId] = (this.peerShares[sessionId] ?? []).filter((s) => s.person !== person);
  }

  async loadStatus(sessionId: string): Promise<void> {
    try {
      this.shareStatus[sessionId] = await getShareStatus(sessionId);
    } catch {
      this.shareStatus[sessionId] = null;
    }
  }
}

export const contactsState = new ContactsState();
