import { syncState } from '$lib/stores/sync.svelte.js';

/** An LLM provider as people read it: `peer:<sync id>` (a paired computer's models, backend
 * summarization/peer.py) is that computer's name. */
export function providerLabel(provider: string): string {
  if (!provider.startsWith('peer:')) return provider;
  const id = provider.slice('peer:'.length);
  return syncState.peers.find((p) => p.id === id)?.name ?? 'a paired computer';
}
