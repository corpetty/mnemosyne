import { listUsers } from '$lib/api/backend.js';
import type { TeamStatus, UserInfo } from '$lib/types/index.js';

/** Everyone on a team server (names and roles), for whose a meeting is and whom to share it with. */
class TeamState {
  people = $state<UserInfo[]>([]);
  /** Where others reach this computer when it is shared (routes/team.py), for invite links. */
  address = $state('');
  /** This computer's team-server status (routes/team.py), shared by the two parts of the Devices and people tab that show it. */
  host = $state<TeamStatus | null>(null);

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
