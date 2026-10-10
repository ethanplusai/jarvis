import { test } from "node:test";
import assert from "node:assert/strict";
import {
  PANEL_KEY, clampPosition, loadPanelState, savePanelState, formatStamp, stampTitle, nudge,
} from "../src/panelstate.ts";

function storage(initial: Record<string, string> = {}) {
  const map = new Map(Object.entries(initial));
  return {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => { map.set(k, v); },
    map,
  };
}

// ── where the panel may sit ────────────────────────────────────────────────

test("a position inside the viewport is left alone", () => {
  assert.deepEqual(clampPosition(100, 120, { width: 400, height: 300 }, { width: 1400, height: 900 }),
    { left: 100, top: 120 });
});

test("a panel dragged off any edge is pulled back to the margin", () => {
  const panel = { width: 400, height: 300 };
  const view = { width: 1400, height: 900 };
  assert.deepEqual(clampPosition(-50, -20, panel, view), { left: 8, top: 8 });
  assert.deepEqual(clampPosition(1300, 800, panel, view), { left: 1400 - 400 - 8, top: 900 - 300 - 8 });
});

test("a panel wider than the viewport is pinned to the left margin, never lost off the right", () => {
  assert.deepEqual(clampPosition(300, 10, { width: 500, height: 100 }, { width: 390, height: 844 }),
    { left: 8, top: 10 });
});

test("a saved position from a bigger screen lands inside a smaller one", () => {
  const { left, top } = clampPosition(1600, 1000, { width: 420, height: 500 }, { width: 390, height: 844 });
  assert.ok(left >= 0 && left + 420 <= 390 + 420);   // pinned left, not off-screen
  assert.equal(left, 8);
  assert.equal(top, 844 - 500 - 8);
});

// ── remembering it ──────────────────────────────────────────────────────────

test("the default is the CSS position, expanded", () => {
  assert.deepEqual(loadPanelState(storage()), { left: null, top: null, minimized: false });
  assert.deepEqual(loadPanelState(null), { left: null, top: null, minimized: false });
});

test("a saved state comes back, and garbage or a blocked storage does not crash", () => {
  const s = storage();
  savePanelState(s, { left: 40, top: 60, minimized: true });
  assert.deepEqual(loadPanelState(s), { left: 40, top: 60, minimized: true });
  assert.ok(s.map.has(PANEL_KEY));
  assert.deepEqual(loadPanelState(storage({ [PANEL_KEY]: "{not json" })),
    { left: null, top: null, minimized: false });
  assert.deepEqual(loadPanelState(storage({ [PANEL_KEY]: JSON.stringify({ left: "x", top: 1e9, minimized: "yes" }) })),
    { left: null, top: 1e9, minimized: true });
  const throwing = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } };
  assert.deepEqual(loadPanelState(throwing), { left: null, top: null, minimized: false });
  assert.doesNotThrow(() => savePanelState(throwing, { left: 1, top: 2, minimized: false }));
});

// ── the clock on every message ──────────────────────────────────────────────

function at(y: number, mo: number, d: number, h: number, mi: number, s: number): number {
  return new Date(y, mo - 1, d, h, mi, s).getTime() / 1000;   // local time, like the browser
}

test("a message from today shows hours, minutes and seconds, zero-padded", () => {
  const now = at(2026, 9, 24, 14, 5, 9);
  assert.equal(formatStamp(at(2026, 9, 24, 9, 7, 3), now), "09:07:03");
  assert.equal(formatStamp(at(2026, 9, 24, 23, 59, 59), now), "23:59:59");
});

test("a message from another day carries the day as well", () => {
  const now = at(2026, 9, 24, 14, 5, 9);
  const text = formatStamp(at(2026, 9, 23, 22, 41, 0), now);
  assert.match(text, /22:41:00$/);
  assert.match(text, /23/);
  assert.ok(text.length > "22:41:00".length, text);
});

test("no timestamp is an empty string, never 1970", () => {
  assert.equal(formatStamp(undefined as unknown as number, 0), "");
  assert.equal(formatStamp(0, 0), "");
  assert.equal(formatStamp(Number.NaN, 0), "");
});

test("the hover title is the full date and time", () => {
  const title = stampTitle(at(2026, 9, 23, 22, 41, 0));
  assert.match(title, /2026/);
  assert.match(title, /22:41/);
});

// ── moving it without a mouse ───────────────────────────────────────────────

test("arrow keys nudge by a step and other keys do nothing", () => {
  assert.deepEqual(nudge("ArrowLeft"), { dx: -20, dy: 0 });
  assert.deepEqual(nudge("ArrowRight"), { dx: 20, dy: 0 });
  assert.deepEqual(nudge("ArrowUp"), { dx: 0, dy: -20 });
  assert.deepEqual(nudge("ArrowDown"), { dx: 0, dy: 20 });
  assert.equal(nudge("Enter"), null);
  assert.equal(nudge("a"), null);
});

test("each panel remembers under its own key", () => {
  const s = storage();
  savePanelState(s, { left: 1, top: 2, minimized: false }, "jarvis-business-panel-v1");
  assert.deepEqual(loadPanelState(s, "jarvis-business-panel-v1"), { left: 1, top: 2, minimized: false });
  assert.deepEqual(loadPanelState(s), { left: null, top: null, minimized: false },
    "the conversation's own slot is untouched");
});
