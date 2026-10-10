// How JARVIS listens: only while a key is held (the default), or with the
// microphone open (a choice).
//
// Measured live: with the microphone always open he transcribed another
// assistant's spoken report from the same room, took it as a barge-in
// mid-sentence and ran a whole turn on it. Echo suppression knows his own
// voice and nobody else's. Hold-to-talk makes "something is being said" and
// "the user is speaking to JARVIS" the same event again.
//
// Pure: no DOM, no socket. The page asks these questions; node:test holds
// the answers (test/listenmode.test.ts).

export const LISTEN_KEY = "jarvis-listen-mode-v1";
export type ListenMode = "hold" | "open";
export const DEFAULT_MODE: ListenMode = "hold";
/** After the key comes up the recogniser stays on this long, so the last
 * word of the sentence lands before the microphone is paused. */
export const TALK_TAIL_MS = 1000;

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export function loadListenMode(storage: StorageLike | null | undefined): ListenMode {
  try {
    return storage?.getItem(LISTEN_KEY) === "open" ? "open" : DEFAULT_MODE;
  } catch {
    return DEFAULT_MODE;
  }
}

export function saveListenMode(storage: StorageLike | null | undefined, mode: ListenMode): void {
  try {
    storage?.setItem(LISTEN_KEY, mode);
  } catch {
    // Blocked storage: the choice lasts for this page and no longer.
  }
}

const INTERACTIVE = new Set(["INPUT", "TEXTAREA", "SELECT", "BUTTON", "A", "SUMMARY"]);

/** True for anything that owns its own keys: a text box (the letters are
 * the message), a select, a button or link (Space and Enter activate it). */
export function isInteractiveTarget(target: { tagName?: string; isContentEditable?: boolean } | null | undefined): boolean {
  if (!target) return false;
  if (target.isContentEditable) return true;
  return INTERACTIVE.has(String(target.tagName || "").toUpperCase());
}

export type Shortcut = "talk-start" | "talk-stop" | "mic" | "voice" | null;

export interface KeyLike {
  type: "keydown" | "keyup" | string;
  key: string;
  repeat?: boolean;
  ctrlKey?: boolean;
  metaKey?: boolean;
  altKey?: boolean;
}

/** What a key event means on the voice page, or null. `typing` is
 * `isInteractiveTarget(event.target)`: while it is true nothing here is a
 * shortcut. A chord (Ctrl/Cmd/Alt) belongs to the browser. */
export function shortcutFor(event: KeyLike, typing: boolean): Shortcut {
  if (typing || event.ctrlKey || event.metaKey || event.altKey) return null;
  if (event.key === " ") {
    if (event.type === "keyup") return "talk-stop";
    if (event.type === "keydown" && !event.repeat) return "talk-start";
    return null;
  }
  if (event.type !== "keydown") return null;
  if (event.key === "m" || event.key === "M") return "mic";
  if (event.key === "v" || event.key === "V") return "voice";
  return null;
}

/** The idle status line: how to be heard right now. */
export function hintFor(mode: ListenMode, muted: boolean): string {
  if (muted) return "microphone muted — press M";
  return mode === "hold" ? "hold Space to talk" : "";
}
