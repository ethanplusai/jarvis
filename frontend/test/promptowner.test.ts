import { test } from "node:test";
import assert from "node:assert/strict";
import { handFor, hostWaitLine, startedAs } from "../src/dashboard/promptowner.ts";

// The origins session_watch.py can put on a row, and nothing else.
const ORIGINS = ["terminal", "desktop", "editor", "remote", "background", "other"];

test("only a terminal session is told to press a key", () => {
  // Measured live: a session Paperclip drove through the Agent SDK was badged
  // "your keystroke" — "JARVIS cannot answer this one — it wants a key pressed
  // in that terminal" — and it had no terminal.
  for (const origin of ORIGINS) {
    const hand = handFor(origin);
    const says = `${hand.pill} ${hand.note}`.toLowerCase();
    if (origin === "terminal") {
      assert.match(says, /key/);
      assert.match(says, /its terminal/);
    } else {
      assert.doesNotMatch(says, /keystroke|key pressed/, origin);
    }
  }
});

test("each person's prompt says where it is shown", () => {
  assert.match(handFor("desktop").note, /Claude desktop app/);
  assert.match(handFor("editor").note, /your editor/);
  assert.match(handFor("remote").note, /Claude app it was started from/);
});

test("a program's prompt is the program's, and is not the loud red", () => {
  const hand = handFor("background");
  assert.match(hand.note, /program that started this session/);
  assert.match(hand.note, /Only that program can answer it/);
  assert.equal(hand.tone, "warn");
});

test("an origin the table does not know claims nothing it cannot back", () => {
  for (const unknown of ["other", "", "vscode", "<b>terminal</b>"]) {
    const hand = handFor(unknown);
    assert.equal(hand.pill, "your hand", unknown);
    assert.match(hand.note, /wherever it was started/);
  }
});

test("a host wait says what it is paused on, and that nothing is asked", () => {
  const line = hostWaitLine("permission prompt");
  assert.match(line, /permission prompt/);
  assert.match(line, /program that started it answers/);
  assert.match(line, /Nothing is asked of you/);
});

test("an unnamed host wait invents no reason", () => {
  const line = hostWaitLine("");
  assert.match(line, /paused on the program that started it/);
  assert.doesNotMatch(line, /“”|""/);
});

test("started-as shows the derived origin beside the roster's own word", () => {
  assert.equal(startedAs("background", "sdk-ts"), "background · sdk-ts");
  assert.equal(startedAs("desktop", "claude-desktop"), "desktop · claude-desktop");
  assert.equal(startedAs("terminal", ""), "terminal", "an older row has no entrypoint");
  assert.equal(startedAs("terminal", undefined), "terminal", "nor does an older server");
});
