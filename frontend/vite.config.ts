import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { normalizeApiBaseUrl } from "./src/api/baseUrl";

// This config is ESM, so __dirname does not exist here.
const frontendRoot = dirname(fileURLToPath(import.meta.url));

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
      // Setting `allow` replaces Vite's defaults, so the frontend root has to
      // be listed explicitly -- without it the dev server refuses to serve the
      // app's own modules. The only thing needed from outside it is the
      // generated API contract; the rest of the repository (local databases,
      // the packaging runtime, .env files) has no business being served, even
      // on loopback, which `[".."]` previously allowed.
      fs: { allow: [frontendRoot, resolve(frontendRoot, "..", "contracts")] },
    },
    build: {
      outDir: "dist",
      emptyOutDir: true,
    },
  };
});
