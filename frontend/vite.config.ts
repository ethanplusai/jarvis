import { defineConfig } from "vite";

export default defineConfig({
  server: {
    port: 5180,
    proxy: {
      "/ws": {
        target: "http://localhost:8340",
        ws: true,
        secure: false,
      },
      "/api": {
        target: "http://localhost:8340",
        secure: false,
      },
    },
  },
  build: {
    outDir: "dist",
  },
});
