import { connectionState } from '$lib/stores/connection.svelte.js';

/**
 * May the signed-in person change this meeting? The backend's rule (access.py): the desktop
 * app and admins change everything, everyone else only their own meetings. A reviewer reads an
 * member's meeting without the controls that would only be refused.
 */
export function canChange(session: { owner_id: string } | null | undefined): boolean {
  if (!session) return false;
  const me = connectionState.me;
  return !me?.id || me.role === 'admin' || session.owner_id === me.id;
}
