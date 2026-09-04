import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:4174",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 4174",
    url: "http://127.0.0.1:4174",
    // Locally, reusing a dev server you already have running is the point.
    // In CI it means silently testing whatever happens to be listening on
    // 4174 instead of this checkout, so fail there rather than guess.
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
