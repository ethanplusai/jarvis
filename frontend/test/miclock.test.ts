import { test } from "node:test";
import assert from "node:assert/strict";
import { claimMicrophone, MIC_LOCK, type LockManagerLike } from "../src/miclock";

/** A lock manager the shape of navigator.locks, for one name, in memory. */
function fakeLocks(): LockManagerLike & { holders: number; waiters: number } {
  let held = false;
  const queue: Array<() => void> = [];
  const mgr = {
    holders: 0,
    waiters: 0,
    async request(name: string, options: { ifAvailable: boolean }, callback: (lock: unknown | null) => Promise<void>) {
      assert.equal(name, MIC_LOCK);
      if (held) {
        if (options.ifAvailable) { await callback(null); return; }
        mgr.waiters++;
        await new Promise<void>((resolve) => queue.push(resolve));
        mgr.waiters--;
      }
      held = true; mgr.holders++;
      try { await callback({ name }); }
      finally { held = false; mgr.holders--; const next = queue.shift(); if (next) next(); }
    },
  };
  return mgr;
}

const tick = () => new Promise((r) => setImmediate(r));

test("the first tab gets the microphone at once", async () => {
  const locks = fakeLocks();
  let waited = false;
  const claim = claimMicrophone(locks, () => { waited = true; });
  assert.equal(await claim.granted, true);
  assert.equal(waited, false);
  await tick();
  assert.equal(locks.holders, 1);
  claim.release();
});

test("a second tab is told to wait, and takes over when the first lets go", async () => {
  const locks = fakeLocks();
  const first = claimMicrophone(locks, () => assert.fail("first should not wait"));
  await first.granted; await tick();
  let waited = false;
  const second = claimMicrophone(locks, () => { waited = true; });
  await tick(); await tick();
  assert.equal(waited, true, "second tab was told another tab holds it");
  assert.equal(locks.waiters, 1);
  let secondGranted = false;
  second.granted.then(() => { secondGranted = true; });
  await tick();
  assert.equal(secondGranted, false, "not while the first still holds it");
  first.release();
  await tick(); await tick(); await tick();
  assert.equal(await second.granted, true);
  assert.equal(locks.holders, 1, "handed over, never two at once");
  second.release();
});

test("no lock manager means listen anyway", async () => {
  const claim = claimMicrophone(undefined, () => assert.fail("nothing to wait for"));
  assert.equal(await claim.granted, true);
});
