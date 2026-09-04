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
  webServer: [
    // The real API, on deterministic dependencies. Only real-api.spec.ts uses
    // it; every other spec mocks the network and never reaches it.
    {
      command: "python ../scripts/e2e_backend.py --port 8799",
      url: "http://127.0.0.1:8799/api/v1/health/live",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    {
      command: "npm run dev -- --host 127.0.0.1 --port 4174",
      url: "http://127.0.0.1:4174",
      // Locally, reusing a dev server you already have running is the point.
      // In CI it means silently testing whatever happens to be listening on
      // 4174 instead of this checkout, so fail there rather than guess.
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      // Vite proxies /api to the backend above.
      env: { CORTEX_BACKEND_PORT: "8799" },
    },
  ],
});
