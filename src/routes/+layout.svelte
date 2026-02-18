<script lang="ts">
	import '../app.css';

	let { children } = $props();
</script>

<svelte:head>
	<title>Mnemosyne</title>
</svelte:head>

{@render children()}
