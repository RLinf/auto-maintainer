import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backend = process.env.AM_BACKEND ?? "http://127.0.0.1:7788";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/auto_maintainer/web/static",
    emptyOutDir: true,
    assetsDir: "assets",
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: backend, changeOrigin: false },
      "/auth": { target: backend, changeOrigin: false },
      "/healthz": { target: backend, changeOrigin: false },
    },
  },
});
