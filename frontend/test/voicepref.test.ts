import { test } from "node:test";
import assert from "node:assert/strict";
import { VOICE_KEY, loadVoiceOn, saveVoiceOn, voiceFrame } from "../src/voicepref.ts";

function storage(initial: Record<string, string> = {}) {
  const map = new Map(Object.entries(initial));
  return {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => { map.set(k, v); },
    map,
  };
}

test("his voice is on until the user turns it off", () => {
  assert.equal(loadVoiceOn(storage()), true);
  assert.equal(loadVoiceOn(null), true);
});

test("the choice survives a reload", () => {
  const s = storage();
  saveVoiceOn(s, false);
  assert.equal(loadVoiceOn(s), false);
  saveVoiceOn(s, true);
  assert.equal(loadVoiceOn(s), true);
  assert.equal(s.map.has(VOICE_KEY), true);
});

test("a broken or blocked storage is not a crash and not a mute", () => {
  const throwing = {
    getItem: () => { throw new Error("blocked"); },
    setItem: () => { throw new Error("blocked"); },
  };
  assert.equal(loadVoiceOn(throwing), true);
  assert.doesNotThrow(() => saveVoiceOn(throwing, false));
  assert.equal(loadVoiceOn(storage({ [VOICE_KEY]: "garbage" })), true);
});

test("the frame the server reads is one boolean", () => {
  assert.deepEqual(voiceFrame(false), { type: "voice", on: false });
  assert.deepEqual(voiceFrame(true), { type: "voice", on: true });
});
