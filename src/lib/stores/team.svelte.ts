import { listUsers } from '$lib/api/backend.js';
import type { UserInfo } from '$lib/types/index.js';

/** Everyone on a team server (names and roles), for whose a meeting is and whom to share it with. */
class TeamState {
  people = $state<UserInfo[]>([]);

  async load() {
    try {
      this.people = await listUsers();
    } catch {
      this.people = [];
    }
  }

  name(id: string): string {
    return this.people.find((p) => p.id === id)?.name ?? '';
  }
}

export const teamState = new TeamState();
