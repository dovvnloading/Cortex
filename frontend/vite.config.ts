import { copyFileSync, mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

function syncCortexIcon() {
  const source = fileURLToPath(new URL("../assets/cortex.svg", import.meta.url));
  const targetDirectory = fileURLToPath(new URL("./public/", import.meta.url));
  const target = fileURLToPath(new URL("./public/cortex.svg", import.meta.url));

  return {
    name: "sync-cortex-icon",
    buildStart() {
      mkdirSync(targetDirectory, { recursive: true });
      copyFileSync(source, target);
    },
  };
}

export default defineConfig({
  plugins: [syncCortexIcon(), react()],
  server: {
    port: Number(process.env.CORTEX_FRONTEND_PORT ?? 5173),
    proxy: {
      "/api": {
        target: `http://127.0.0.1:${process.env.CORTEX_BACKEND_PORT ?? 8765}`,
      },
    },
    fs: { allow: [".."] },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
