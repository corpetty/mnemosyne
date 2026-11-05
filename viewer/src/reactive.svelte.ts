// Test helper: an object whose fields are reactive, to pass as a mounted component's props and
// change afterwards (runes only work in .svelte.ts files).
export function reactive<T extends object>(init: T): T {
	const props = $state(init);
	return props;
}
