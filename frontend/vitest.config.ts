import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    restoreMocks: true,
    clearMocks: true,
    // The default (5000ms) leaves no headroom over an inner explicit
    // findByRole timeout of the same size (App.test.tsx's lazy chat-route
    // recovery test), so the whole test can time out at the same moment the
    // inner wait legitimately would under full-suite load rather than only
    // when something is actually wrong.
    testTimeout: 15_000,
  },
});
