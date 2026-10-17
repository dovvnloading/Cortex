import { readFileSync } from "node:fs";

/**
 * Where the real-API backend listens and how it names itself.
 *
 * `e2e-backend.json` is the one source: `playwright.config.ts` starts the
 * server on this port and points the dev server's proxy at it, and
 * `scripts/e2e_backend.py` reads the same file for its default port and the
 * marker it puts on its liveness route. The port is its own -- the README
 * screenshot server defaults to 8799 -- and the marker lets the real-API spec
 * refuse a server left running by something else instead of testing it.
 */
interface E2EBackendIdentity {
  port: number;
  markerHeader: string;
  marker: string;
}

export const E2E_BACKEND: E2EBackendIdentity = JSON.parse(
  readFileSync(new URL("./e2e-backend.json", import.meta.url), "utf8"),
);

export const E2E_BACKEND_ORIGIN = `http://127.0.0.1:${E2E_BACKEND.port}`;

/** The route both the readiness check and the marker check use. */
export const E2E_BACKEND_LIVENESS = "/api/v1/health/live";
