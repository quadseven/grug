import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Web unit tests (grug#1206). jsdom stands in for the browser; every test
// mocks `fetch`, so nothing here talks to a real API.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["src/test/setup.ts"],
  },
});
