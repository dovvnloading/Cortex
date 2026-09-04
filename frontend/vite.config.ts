import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

import { normalizeApiBaseUrl } from "./src/api/baseUrl";

function cortexDevIdentityPlugin(nonce: string | undefined): Plugin {
  return {
    name: "cortex-dev-identity",
    configureServer(server) {
      if (!nonce) return;
      server.middlewares.use((_request, response, next) => {
        response.setHeader("X-Cortex-Dev-Server", nonce);
        next();
      });
    },
  };
}

export default defineConfig(({ mode }) => {
  // Vite embeds VITE_* values into the production bundle. Fail before the
  // bundle is produced if that value would send authenticated local data to a
  // remote origin; the client repeats the check at runtime for stale bundles.
  normalizeApiBaseUrl(process.env.VITE_API_BASE_URL, mode === "production");

  return {
    plugins: [react(), cortexDevIdentityPlugin(process.env.CORTEX_DEV_SERVER_NONCE)],
    server: {
      port: Number(process.env.CORTEX_FRONTEND_PORT ?? 5173),
      proxy: {
        "/api": {
          target: `http://127.0.0.1:${process.env.CORTEX_BACKEND_PORT ?? 8765}`,
        },
      },
      // The frontend imports the generated API types from contracts/, which
      // lives outside this root. Allow that one directory rather than the
      // entire repository -- everything else up there (local databases, the
      // packaging runtime, .env files) has no business being served, even on
      // loopback.
      fs: { allow: [resolve(__dirname, "..", "contracts")] },
    },
    build: {
      outDir: "dist",
      emptyOutDir: true,
    },
  };
});
