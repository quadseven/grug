import { defineConfig } from "vite";
import { resolve } from "node:path";
import react from "@vitejs/plugin-react";

// SPA entry is `app.html`, not `index.html`. The static landing
// (`public/index.html`) owns `/` so the canonical URL stays `grug.lol/`
// instead of being 301'd to `/Grug` by Cloudflare Pages "Pretty URLs"
// stripping the `.html` from any rewrite target that resolves to a
// real file. SPA routes are wired in `public/_redirects` to rewrite
// onto `/app.html`.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    rollupOptions: {
      input: resolve(__dirname, "app.html"),
    },
    // `hidden` emits .map files for Seer/error-monitoring upload but
    // doesn't add the `//# sourceMappingURL=` comment, so DevTools
    // won't auto-load + expose the TS source. review-bot P2 on PR #42.
    sourcemap: "hidden",
  },
  server: {
    port: 5173,
    strictPort: true,
    open: "/app.html",
  },
});
