import { sveltekit } from "@sveltejs/kit/vite";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [tailwindcss(), sveltekit()],
  server: {
    port: 5173,
    strictPort: true,
    // The Publish dialog renders its preview with the publication viewer's own component
    // (viewer/src/Meeting.svelte), outside SvelteKit's default allow list.
    fs: { allow: ["viewer"] },
  },
});
