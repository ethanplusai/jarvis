import { existsSync } from "node:fs";
import { resolve } from "node:path";

/** Match server.py's certificate detection; never depend on the shell's cwd. */
export function backendTarget(root: string, override?: string): string {
  if (override) {
    const url = new URL(override);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password ||
        url.pathname !== "/" || url.search || url.hash) {
      throw new Error("JARVIS_BACKEND_URL must be an HTTP(S) origin without credentials or a path.");
    }
    return url.origin;
  }
  const tls = existsSync(resolve(root, "cert.pem")) && existsSync(resolve(root, "key.pem"));
  return `${tls ? "https" : "http"}://127.0.0.1:8340`;
}
