import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
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
