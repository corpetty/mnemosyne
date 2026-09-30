import type { ClientFact } from '$lib/types/index.js';

/** Client facts (advisory summaries) by kind, in the order they are shown. */
export const FACT_HEADINGS: [ClientFact['kind'], string][] = [
  ['goal', 'Goals'],
  ['life_event', 'Life events'],
  ['income_change', 'Income changes'],
  ['risk_tolerance', 'Risk tolerance'],
  ['account', 'Accounts'],
  ['beneficiary', 'Beneficiaries'],
  ['insurance', 'Insurance'],
  ['estate', 'Estate'],
  ['next_review', 'Next review'],
  ['other', 'Other']
];

/** Facts grouped under their headings, empty groups left out. */
export function groupFacts<F extends { kind: ClientFact['kind'] }>(facts: F[]): { kind: ClientFact['kind']; heading: string; facts: F[] }[] {
  return FACT_HEADINGS.map(([kind, heading]) => ({ kind, heading, facts: facts.filter((f) => f.kind === kind) })).filter((g) => g.facts.length);
}
