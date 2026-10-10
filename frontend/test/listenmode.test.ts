import { test } from "node:test";
import assert from "node:assert/strict";
import {
  LISTEN_KEY, DEFAULT_MODE, TALK_TAIL_MS, loadListenMode, saveListenMode,
  isInteractiveTarget, shortcutFor, hintFor,
} from "../src/listenmode.ts";

function storage(initial: Record<string, string> = {}) {
  const map = new Map(Object.entries(initial));
  return { getItem: (k: string) => map.get(k) ?? null, setItem: (k: string, v: string) => { map.set(k, v); }, map };
}

// ── the mode ────────────────────────────────────────────────────────────────

test("hold-to-talk is the default: an open microphone is a choice", () => {
  assert.equal(DEFAULT_MODE, "hold");
  assert.equal(loadListenMode(storage()), "hold");
  assert.equal(loadListenMode(null), "hold");
  assert.equal(loadListenMode(storage({ [LISTEN_KEY]: "garbage" })), "hold");
});

test("the choice survives a reload and a blocked storage is not a crash", () => {
  const s = storage();
  saveListenMode(s, "open");
  assert.equal(loadListenMode(s), "open");
  saveListenMode(s, "hold");
  assert.equal(loadListenMode(s), "hold");
  const throwing = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } };
  assert.equal(loadListenMode(throwing), "hold");
  assert.doesNotThrow(() => saveListenMode(throwing, "open"));
});

test("the tail after the key comes up is long enough for the last word to land", () => {
  assert.ok(TALK_TAIL_MS >= 800 && TALK_TAIL_MS <= 2000, String(TALK_TAIL_MS));
});

// ── where keys are keys and where they are typing ──────────────────────────

test("a text box, a select, a button and a link are never stolen from", () => {
  for (const tagName of ["INPUT", "TEXTAREA", "SELECT", "BUTTON", "A", "SUMMARY"]) {
    assert.equal(isInteractiveTarget({ tagName, isContentEditable: false }), true, tagName);
  }
  assert.equal(isInteractiveTarget({ tagName: "DIV", isContentEditable: true }), true);
  assert.equal(isInteractiveTarget({ tagName: "BODY", isContentEditable: false }), false);
  assert.equal(isInteractiveTarget({ tagName: "CANVAS", isContentEditable: false }), false);
  assert.equal(isInteractiveTarget(null), false);
});

// ── the shortcuts ───────────────────────────────────────────────────────────

test("Space held is talk; Space released is the end of it; a repeat is neither", () => {
  assert.equal(shortcutFor({ type: "keydown", key: " ", repeat: false }, false), "talk-start");
  assert.equal(shortcutFor({ type: "keydown", key: " ", repeat: true }, false), null);
  assert.equal(shortcutFor({ type: "keyup", key: " " }, false), "talk-stop");
});

test("M is the microphone, V is his voice, either case", () => {
  assert.equal(shortcutFor({ type: "keydown", key: "m" }, false), "mic");
  assert.equal(shortcutFor({ type: "keydown", key: "M" }, false), "mic");
  assert.equal(shortcutFor({ type: "keydown", key: "v" }, false), "voice");
  assert.equal(shortcutFor({ type: "keyup", key: "m" }, false), null, "a toggle fires once, on the way down");
});

test("while typing, nothing is a shortcut — the letters are the message", () => {
  assert.equal(shortcutFor({ type: "keydown", key: " ", repeat: false }, true), null);
  assert.equal(shortcutFor({ type: "keydown", key: "m" }, true), null);
  assert.equal(shortcutFor({ type: "keyup", key: " " }, true), null);
});

test("a chord is somebody else's shortcut", () => {
  assert.equal(shortcutFor({ type: "keydown", key: "m", ctrlKey: true }, false), null);
  assert.equal(shortcutFor({ type: "keydown", key: "v", metaKey: true }, false), null);
  assert.equal(shortcutFor({ type: "keydown", key: " ", altKey: true, repeat: false }, false), null);
});

test("other keys are nothing", () => {
  assert.equal(shortcutFor({ type: "keydown", key: "a" }, false), null);
  assert.equal(shortcutFor({ type: "keydown", key: "Enter" }, false), null);
});

// ── what the idle status line says ──────────────────────────────────────────

test("the idle line tells the user how to be heard", () => {
  assert.match(hintFor("hold", false), /hold Space/);
  assert.equal(hintFor("open", false), "");
  assert.match(hintFor("hold", true), /muted/);
  assert.match(hintFor("open", true), /muted/);
});
