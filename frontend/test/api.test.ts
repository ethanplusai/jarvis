import { test } from "node:test";
import assert from "node:assert/strict";
import { ApiError, cancelRun, retryRun, listActiveRuns, runTokens, type RunRow } from "../src/dashboard/api.ts";

test("cancel and retry reject unsuccessful HTTP responses", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response('{"detail":"denied"}', { status: 403 }));
  await assert.rejects(cancelRun("r1"), (e: unknown) => e instanceof ApiError && e.status === 403);
  await assert.rejects(retryRun("r1"), (e: unknown) => e instanceof ApiError && e.status === 403);
});

test("retry requires a usable run identifier", async (t) => {
  t.mock.method(globalThis, "fetch", async () => Response.json({}));
  await assert.rejects(retryRun("r1"));
});

test("successful actions preserve their API contracts", async (t) => {
  const calls: [string, string | undefined][] = [];
  t.mock.method(globalThis, "fetch", async (url: string, init: RequestInit) => {
    calls.push([url, init.method]);
    return init.method === "DELETE" ? new Response(null, { status: 204 }) : Response.json({ run_id: "r2" });
  });
  await cancelRun("r1");
  assert.equal(await retryRun("r1"), "r2");
  assert.deepEqual(calls, [["/api/runs/r1", "DELETE"], ["/api/runs/r1/retry", "POST"]]);
});

test("active runs paginate independently using a timestamp and ID cursor", async (t) => {
  const calls: URL[] = [];
  const first = Array.from({ length: 200 }, (_, i) => ({ id: `r${i}`, created_at: 42 }));
  t.mock.method(globalThis, "fetch", async (value: string) => {
    const url = new URL(value, "http://localhost");
    calls.push(url);
    return Response.json({ runs: calls.length === 1 ? first : [{ id: "old", created_at: 1 }] });
  });
  assert.equal((await listActiveRuns()).length, 201);
  assert.equal(calls[0].searchParams.get("status"), "queued,running");
  assert.equal(calls[1].searchParams.get("before"), "42");
  assert.equal(calls[1].searchParams.get("before_id"), "r199");
});

test("run tokens include both cache categories", () => {
  assert.equal(runTokens({ input_tokens: 1, output_tokens: 2, cache_read_tokens: 100,
    cache_creation_tokens: 20 } as RunRow), 123);
});

test("reindexing memory is a POST and returns what was indexed", async (t) => {
  const { reindexMemory } = await import("../src/dashboard/api.ts");
  const calls: [string, string | undefined][] = [];
  t.mock.method(globalThis, "fetch", async (url: string, init: RequestInit) => {
    calls.push([url, init?.method]);
    return Response.json({ indexed: ["starnet-station"], left_out: [], full: false });
  });
  const result = await reindexMemory();
  assert.deepEqual(calls, [["/api/memory/reindex", "POST"]]);
  assert.deepEqual(result.indexed, ["starnet-station"]);
});

test("a refused reindex is an ApiError with its status", async (t) => {
  const { reindexMemory } = await import("../src/dashboard/api.ts");
  t.mock.method(globalThis, "fetch", async () => new Response("no", { status: 403 }));
  await assert.rejects(reindexMemory(), (e: unknown) => e instanceof ApiError && e.status === 403);
});
