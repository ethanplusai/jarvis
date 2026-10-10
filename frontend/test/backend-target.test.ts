import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { backendTarget } from "../backend-target.ts";

test("proxy matches the backend with no, partial, or complete certificates", () => {
  const dir = mkdtempSync(join(tmpdir(), "jarvis-proxy-"));
  try {
    assert.equal(backendTarget(dir), "http://127.0.0.1:8340");
    writeFileSync(join(dir, "cert.pem"), "test");
    assert.equal(backendTarget(dir), "http://127.0.0.1:8340");
    writeFileSync(join(dir, "key.pem"), "test");
    assert.equal(backendTarget(dir), "https://127.0.0.1:8340");
    assert.equal(backendTarget(dir, "http://localhost:9000/"), "http://localhost:9000");
  } finally { rmSync(dir, { recursive: true }); }
});

test("invalid overrides fail at startup instead of producing proxy failures", () => {
  for (const value of ["wat", "file:///tmp", "http://user:password@localhost", "http://localhost/api", "http://localhost/?x=y"]) {
    assert.throws(() => backendTarget(".", value));
  }
});
