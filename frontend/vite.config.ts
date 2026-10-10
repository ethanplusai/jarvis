import { defineConfig } from "vite";
import { resolve } from "path";
import { backendTarget } from "./backend-target";

const target = backendTarget(resolve(__dirname, ".."), process.env.JARVIS_BACKEND_URL);

// The same headers server.py's OriginGuard puts on everything it serves.
// They have to be here too: in development the pages come from Vite, not
// from the API port, and OriginGuard trusts an Origin of localhost:5173. A
// hostile page that iframes this dev server and harvests a click gets a
// proxied POST carrying that trusted Origin -- the exact exploit the guard
// exists to stop, routed around it. Proxied /api responses already carry
// these from JARVIS; the HTML itself did not.
const SECURITY_HEADERS = {
  "X-Frame-Options": "DENY",
  "Content-Security-Policy": "frame-ancestors 'none'",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
};

export default defineConfig({
  server: {
    port: 5173,
    strictPort: true,
    headers: SECURITY_HEADERS,
    proxy: {
      "/ws": {
        target,
        ws: true,
        secure: false,
      },
      "/api": {
        target,
        secure: false,
      },
    },
  },
  preview: {
    headers: SECURITY_HEADERS,
  },
  build: {
    outDir: "dist",
    rollupOptions: {
      input: {
        main: resolve(__dirname, "index.html"),
        dashboard: resolve(__dirname, "dashboard.html"),
      },
    },
  },
});
