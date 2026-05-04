import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    // `hidden` emits .map files for Sentry/error-monitoring upload but
    // doesn't add the `//# sourceMappingURL=` comment, so DevTools
    // won't auto-load + expose the TS source. Greptile P2 on PR #42.
    sourcemap: "hidden",
  },
  server: {
    port: 5173,
    strictPort: true,
  },
});
