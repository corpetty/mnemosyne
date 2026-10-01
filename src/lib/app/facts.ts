import type { ClientFact } from '$lib/types/index.js';

/** Client facts (client-style summaries) by kind, in the order they are shown. */
export const FACT_HEADINGS: [ClientFact['kind'], string][] = [
  ['goal', 'Goals'],
  ['concern', 'Concerns'],
  ['preference', 'Preferences'],
  ['context', 'Context'],
  ['next_meeting', 'Next meeting'],
  ['other', 'Other']
];

/** Facts grouped under their headings, empty groups left out. */
export function groupFacts<F extends { kind: ClientFact['kind'] }>(facts: F[]): { kind: ClientFact['kind']; heading: string; facts: F[] }[] {
  return FACT_HEADINGS.map(([kind, heading]) => ({ kind, heading, facts: facts.filter((f) => f.kind === kind) })).filter((g) => g.facts.length);
}
