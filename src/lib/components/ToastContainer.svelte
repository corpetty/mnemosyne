<script lang="ts">
	import { toastState } from '$lib/stores/toast.svelte.js';

	const typeStyles: Record<string, string> = {
		info: 'bg-gray-800 border-gray-600 text-gray-200',
		success: 'bg-green-900 border-green-700 text-green-200',
		error: 'bg-red-900 border-red-700 text-red-200'
	};
</script>

<!-- Always present, so screen readers announce the first toast too; errors interrupt. -->
<div class="fixed bottom-4 right-4 z-50 flex flex-col gap-2" role="status" aria-live="polite">
		{#each toastState.toasts as toast (toast.id)}
			<div
				class="px-4 py-2 rounded-lg border text-sm shadow-lg max-w-sm {typeStyles[toast.type] ?? typeStyles.info}"
				role={toast.type === 'error' ? 'alert' : undefined}
			>
				<div class="flex items-center justify-between gap-3">
					<span>{toast.message}</span>
					{#if toast.action}
						{@const action = toast.action}
						<button
							onclick={() => {
								action.run();
								toastState.dismiss(toast.id);
							}}
							class="shrink-0 px-2 py-0.5 rounded bg-blue-600 hover:bg-blue-500 text-white text-xs"
						>
							{action.label}
						</button>
					{/if}
					<button
						onclick={() => toastState.dismiss(toast.id)}
						aria-label="Dismiss"
						class="text-gray-400 hover:text-gray-200 text-xs"
					>
						✕
					</button>
				</div>
			</div>
		{/each}
</div>
