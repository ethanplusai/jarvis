import { test } from "node:test";
import assert from "node:assert/strict";
import { SpeechLevelDetector, SPEECH_LEVEL_MIN, SUSTAIN_SAMPLES, WINDOW_SAMPLES } from "../src/speechlevel";

const feed = (d: SpeechLevelDetector, level: number, n: number) => {
  const results: boolean[] = [];
  for (let i = 0; i < n; i++) results.push(d.sample(level));
  return results;
};

test("room tone that would have passed the old fixed threshold is not speech", () => {
  // Measured live: 0.021 to 0.041 with nobody talking, reported as DEAF.
  const d = new SpeechLevelDetector();
  const results = feed(d, 0.03, WINDOW_SAMPLES * 2);
  assert.ok(results.every((r) => r === false), "never speech");
  assert.ok(d.threshold > 0.03, `threshold ${d.threshold} sits above the room`);
});

test("speech well above a quiet room is detected once it is sustained", () => {
  const d = new SpeechLevelDetector();
  feed(d, 0.005, WINDOW_SAMPLES);                  // a quiet room
  const results = feed(d, 0.08, SUSTAIN_SAMPLES);
  assert.deepEqual(results, [false, false, true], "the third consecutive loud sample");
});

test("a single spike is a click or a keypress, not speech", () => {
  const d = new SpeechLevelDetector();
  feed(d, 0.005, WINDOW_SAMPLES);
  assert.equal(d.sample(0.5), false);
  assert.equal(d.sample(0.005), false);
  assert.equal(d.sample(0.5), false);
});

test("the threshold follows a noisier room up, and speech still clears it", () => {
  const d = new SpeechLevelDetector();
  feed(d, 0.05, WINDOW_SAMPLES);                   // a loud fan
  assert.ok(d.threshold >= 0.15, `threshold ${d.threshold} is at least three times the room`);
  assert.ok(feed(d, 0.2, SUSTAIN_SAMPLES).at(-1), "talking over the fan is still speech");
});

test("the threshold never drops below the old minimum, even in a silent room", () => {
  const d = new SpeechLevelDetector();
  feed(d, 0, WINDOW_SAMPLES);
  assert.equal(d.threshold, SPEECH_LEVEL_MIN);
  assert.equal(d.noiseFloor, 0);
});

test("the room is the last ten seconds: an old loud stretch stops counting", () => {
  const d = new SpeechLevelDetector();
  feed(d, 0.1, WINDOW_SAMPLES);                    // a noisy start
  feed(d, 0.005, WINDOW_SAMPLES);                  // then quiet for ten seconds
  assert.equal(d.noiseFloor, 0.005);
  assert.equal(d.threshold, SPEECH_LEVEL_MIN);
});
