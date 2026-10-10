import { test } from "node:test";
import assert from "node:assert/strict";
import { DeafWatch } from "../src/deafwatch";

const opts = { deafAfterMs: 3000, complainEveryMs: 15000, recentLoudMs: 500 };

test("sound with no result for three seconds is deaf, and is said once per fifteen seconds", () => {
  const w = new DeafWatch(0, opts);
  for (let t = 200; t <= 2800; t += 200) { w.loud(t); assert.equal(w.check(t), false, `not yet at ${t}`); }
  w.loud(3200);
  assert.equal(w.check(3200), true);
  w.loud(3400);
  assert.equal(w.check(3400), false, "already complained");
  w.loud(18400);
  assert.equal(w.check(18400), true, "fifteen seconds later, again");
});

test("a result resets the clock", () => {
  const w = new DeafWatch(0, opts);
  w.loud(2900); w.result(2900);
  w.loud(4000);
  assert.equal(w.check(4000), false);
  w.loud(6000);
  assert.equal(w.check(6000), true);
});

test("silence is not deafness: no recent sound, no complaint", () => {
  const w = new DeafWatch(0, opts);
  w.loud(1000);
  assert.equal(w.check(10000), false, "the loud sample was long ago");
});

test("while held, his own voice through the speakers counts for nothing", () => {
  // The exact shape that lost every second command: JARVIS speaks for six
  // seconds, the recogniser is paused, then the user talks.
  const w = new DeafWatch(0, opts);
  w.hold();
  for (let t = 200; t <= 6000; t += 200) { w.loud(t); assert.equal(w.check(t), false); }
  w.release(6000);
  // The user starts a sentence 200 ms after he stops; the recogniser needs a
  // moment before its first interim. This must NOT be a restart.
  for (let t = 6200; t <= 8800; t += 200) { w.loud(t); assert.equal(w.check(t), false, `at ${t}`); }
  w.result(8800);                     // the interim arrives
  w.loud(9000);
  assert.equal(w.check(9000), false);
});

test("release starts a clean slate even if it was silent for ages before the hold", () => {
  const w = new DeafWatch(0, opts);
  w.hold();
  w.release(60000);
  w.loud(60200);
  assert.equal(w.check(60200), false);
  assert.equal(w.silentFor(60200), 200);
});
