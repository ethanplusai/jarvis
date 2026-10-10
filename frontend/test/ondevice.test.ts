import { test } from "node:test";
import assert from "node:assert/strict";
import { planLocalRecognition, shouldAbandonLocal } from "../src/ondevice";

test("an installed language pack means recognise locally, now", () => {
  const p = planLocalRecognition("available");
  assert.equal(p.useLocal, true);
  assert.equal(p.install, false);
});

test("a downloadable or downloading pack is installed first, and the cloud is used meanwhile", () => {
  for (const s of ["downloadable", "downloading"] as const) {
    const p = planLocalRecognition(s);
    assert.equal(p.useLocal, false, s);
    assert.equal(p.install, true, s);
  }
});

test("unavailable or unsupported means the cloud, and says which", () => {
  assert.deepEqual(planLocalRecognition("unavailable"), {
    useLocal: false, install: false, say: "on-device recognition: unavailable for en-US on this machine; using the cloud",
  });
  const u = planLocalRecognition("unsupported");
  assert.equal(u.useLocal, false);
  assert.match(u.say, /not supported by this browser/);
});

test("only errors that a restart cannot fix send a local engine back to the cloud", () => {
  assert.equal(shouldAbandonLocal("language-not-supported"), true);
  assert.equal(shouldAbandonLocal("service-not-allowed"), true);
  assert.equal(shouldAbandonLocal("no-speech"), false, "routine");
  assert.equal(shouldAbandonLocal("aborted"), false, "our own rotation");
  assert.equal(shouldAbandonLocal("network"), false, "not a local failure at all");
});
