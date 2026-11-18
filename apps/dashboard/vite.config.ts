import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const apiTarget = process.env.VITE_API_TARGET || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: apiTarget,
        changeOrigin: true,
        ws: true,
      },
      "/health": {
        target: apiTarget,
        changeOrigin: true,
      },
      "/metrics": {
        target: apiTarget,
        changeOrigin: true,
      },
      "/install.sh": {
        target: apiTarget,
        changeOrigin: true,
      },
      "/install.ps1": {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
