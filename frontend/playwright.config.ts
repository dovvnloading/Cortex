import { defineConfig, devices } from "@playwright/test";

import { E2E_BACKEND, E2E_BACKEND_LIVENESS, E2E_BACKEND_ORIGIN } from "./e2e/e2eBackend";

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
    // it; every other spec mocks the network and never reaches it. Its port
    // and liveness marker come from e2e/e2e-backend.json, which
    // scripts/e2e_backend.py reads too. Playwright's readiness probe only
    // sees a status code, so real-api.spec.ts verifies the marker itself and
    // refuses a reused server that does not carry it.
    {
      command: `python ../scripts/e2e_backend.py --port ${E2E_BACKEND.port}`,
      url: `${E2E_BACKEND_ORIGIN}${E2E_BACKEND_LIVENESS}`,
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
      env: { CORTEX_BACKEND_PORT: String(E2E_BACKEND.port) },
    },
  ],
});
