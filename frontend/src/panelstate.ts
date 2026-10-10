// The Conversation panel's own small decisions, kept pure so node:test can
// hold them (test/panelstate.test.ts), the way miclock.ts and voicepref.ts
// are: where the panel may sit, how it is remembered, what a message's
// clock reads, and how far an arrow key moves it.

export const PANEL_KEY = "jarvis-conversation-panel-v1";
export const NUDGE_PX = 20;
export const MARGIN_PX = 8;

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export interface Size { width: number; height: number }

/** `left`/`top` are null until the user has moved it: the stylesheet's
 * corner is the default, and it is the stylesheet that knows about phones. */
export interface PanelState { left: number | null; top: number | null; minimized: boolean }

/**
 * Keep the whole panel on screen, `margin` from every edge. When the panel
 * is wider or taller than the viewport it is pinned to the top-left margin
 * rather than lost off the right or bottom — a saved position from a large
 * screen must still be reachable on a phone.
 */
export function clampPosition(left: number, top: number, panel: Size, viewport: Size,
                              margin = MARGIN_PX): { left: number; top: number } {
  const maxLeft = Math.max(margin, viewport.width - panel.width - margin);
  const maxTop = Math.max(margin, viewport.height - panel.height - margin);
  return {
    left: Math.min(Math.max(left, margin), maxLeft),
    top: Math.min(Math.max(top, margin), maxTop),
  };
}

const DEFAULT: PanelState = { left: null, top: null, minimized: false };

function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** What was saved, field by field: a value that is not a number is the
 * default for that field, not a reason to drop the others. Garbage, a
 * blocked storage or nothing at all is the default panel. */
export function loadPanelState(storage: StorageLike | null | undefined, key = PANEL_KEY): PanelState {
  try {
    const raw = storage?.getItem(key);
    if (!raw) return { ...DEFAULT };
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    if (!parsed || typeof parsed !== "object") return { ...DEFAULT };
    return {
      left: finite(parsed.left),
      top: finite(parsed.top),
      minimized: Boolean(parsed.minimized),
    };
  } catch {
    return { ...DEFAULT };
  }
}

export function savePanelState(storage: StorageLike | null | undefined, state: PanelState,
                               key = PANEL_KEY): void {
  try {
    storage?.setItem(key, JSON.stringify(state));
  } catch {
    // Blocked storage: the position lasts for this page and no longer.
  }
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function pad(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

/**
 * When a message happened, in the viewer's local time: `HH:MM:SS` for
 * today, `D Mon HH:MM:SS` for any other day. An absent or nonsense time is
 * an empty string — never the epoch dressed up as a moment.
 */
export function formatStamp(epochSec: number, nowSec: number = Date.now() / 1000): string {
  if (!Number.isFinite(epochSec) || epochSec <= 0) return "";
  const when = new Date(epochSec * 1000);
  const now = new Date(nowSec * 1000);
  const clock = `${pad(when.getHours())}:${pad(when.getMinutes())}:${pad(when.getSeconds())}`;
  const sameDay = when.getFullYear() === now.getFullYear()
    && when.getMonth() === now.getMonth() && when.getDate() === now.getDate();
  return sameDay ? clock : `${when.getDate()} ${MONTHS[when.getMonth()]} ${clock}`;
}

/** The full date and time for the hover title — `YYYY-MM-DD HH:MM:SS`,
 * local, the same shape everywhere rather than whatever the locale does
 * with a 12-hour clock. */
export function stampTitle(epochSec: number): string {
  if (!Number.isFinite(epochSec) || epochSec <= 0) return "";
  const d = new Date(epochSec * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
    + `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** How far an arrow key moves the panel, or null for any other key. */
export function nudge(key: string, step = NUDGE_PX): { dx: number; dy: number } | null {
  switch (key) {
    case "ArrowLeft": return { dx: -step, dy: 0 };
    case "ArrowRight": return { dx: step, dy: 0 };
    case "ArrowUp": return { dx: 0, dy: -step };
    case "ArrowDown": return { dx: 0, dy: step };
    default: return null;
  }
}
