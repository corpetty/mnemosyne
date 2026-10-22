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

/**
 * Whether this person may comment on the meeting: it is readable by them (the backend only
 * serves meetings they may see), so on the desktop always, and on a team server whenever it is
 * loaded. The backend's `can_comment` on the comment list is the final word.
 */
export function canComment(session: { id: string } | null | undefined): boolean {
  return !!session;
}
