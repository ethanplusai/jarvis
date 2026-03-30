import { defineConfig } from "vite";

export default defineConfig({
  server: {
    port: 5173,
    proxy: {
      "/ws": {
        target: "https://localhost:8340",
        ws: true,
        secure: false,
      },
      "/api": {
        target: "https://localhost:8340",
        secure: false,
      },
    },
  },
  build: {
    outDir: "dist",
  },
});
