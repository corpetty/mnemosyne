/** Svelte action: focus the element when it appears (a dialog's first field). */
export function focusOnMount(node: HTMLElement) {
  queueMicrotask(() => node.focus());
}
