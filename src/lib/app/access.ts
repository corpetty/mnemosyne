import { connectionState } from '$lib/stores/connection.svelte.js';
import { contactsState } from '$lib/stores/contacts.svelte.js';
import { syncState } from '$lib/stores/sync.svelte.js';

/**
 * May the signed-in person change this meeting? The backend's rule (access.py): the desktop
 * app and admins change everything, everyone else only their own meetings. A reviewer reads an
 * member's meeting without the controls that would only be refused.
 */
export function canChange(
  session: { owner_id: string; shared_from?: string } | null | undefined
): boolean {
  if (!session) return false;
  // Shared with me from another person's computer: only what the share allows, never this.
  if (session.shared_from) return false;
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

/**
 * Why a meeting's audio cannot be used here ('' = it can): recorded on another of my
 * computers (`origin`), or shared with me by another person, who keeps the audio.
 */
export function audioReason(
  session: { origin?: string; shared_from?: string } | null | undefined
): string {
  if (session?.shared_from) return `The audio is with ${contactsState.name(session.shared_from)}`;
  if (session?.origin) return syncState.audioOn(session.origin);
  return '';
}
