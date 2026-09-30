import type { Me } from '$lib/types/index.js';

/** Supervision is on (backend services/supervision.py) and this is a reviewer or an admin. */
export function canReview(me: Me | null): boolean {
  return !!me?.supervision && (me.role === 'reviewer' || me.role === 'admin');
}
