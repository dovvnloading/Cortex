import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    // Vitest replaces every stylesheet with an empty module, including a
    // `?raw` import. The design-token tests read the real source of
    // tokens.css (contrast, font-size floor, forced-colors coverage), so that
    // one file is let through. No component imports it, so nothing else in the
    // suite is affected.
    css: { include: [/\/styles\/tokens\.css/] },
    restoreMocks: true,
    clearMocks: true,
    // The default (5000ms) leaves no headroom over an inner explicit
    // findByRole timeout of the same size (App.test.tsx's lazy chat-route
    // recovery test), so the whole test can time out at the same moment the
    // inner wait legitimately would under full-suite load rather than only
    // when something is actually wrong.
    testTimeout: 15_000,
    // Only measured when asked for (`npm run test:coverage`, which CI runs);
    // a plain `npm test` stays as fast as it was.
    coverage: {
      provider: "v8",
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/**/*.test.{ts,tsx}", "src/test/**", "src/**/*.d.ts"],
      reporter: ["text-summary", "lcovonly"],
      reportsDirectory: "./coverage",
      // Each floor is one point under what was measured when it was added
      // (statements 85.9, branches 80.2, functions 84.1, lines 88.8), so a
      // change that drops coverage fails and one that raises it can raise the
      // floor. Never lower one to make a change pass.
      thresholds: {
        statements: 84,
        branches: 79,
        functions: 83,
        lines: 87,
      },
    },
  },
});
