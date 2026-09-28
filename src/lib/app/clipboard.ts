import { toastState } from '$lib/stores/toast.svelte.js';

/** Copy text and say so; a clipboard that refuses (no focus, no permission) says that
 * instead of failing silently in an unhandled rejection. */
export async function copyText(text: string, done: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    toastState.info(done);
    return true;
  } catch {
    toastState.error('Could not copy to the clipboard');
    return false;
  }
}
