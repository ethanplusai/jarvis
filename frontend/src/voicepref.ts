// His voice, off or on — the second toggle beside the microphone's.
//
// The mic button pauses listening and nothing else: muted, he still talks.
// This one silences HIM. Off, the server synthesizes nothing and each
// sentence arrives as a `text` frame the Conversation panel shows at once.
// The choice is the user's and outlives a reload and a server restart, so it
// lives in the browser and is told to the server on every connection.
//
// Pure: no DOM, no socket. Written against a storage-shaped interface so
// the decisions are testable under node:test (see test/voicepref.test.ts),
// the same way miclock.ts and deafwatch.ts are.

export const VOICE_KEY = "jarvis-voice-v1";

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

/** True unless the user turned his voice off. A blocked, missing or garbled
 * storage is "on": a mute nobody asked for is the worse failure. */
export function loadVoiceOn(storage: StorageLike | null | undefined): boolean {
  try {
    return storage?.getItem(VOICE_KEY) !== "off";
  } catch {
    return true;
  }
}

export function saveVoiceOn(storage: StorageLike | null | undefined, on: boolean): void {
  try {
    storage?.setItem(VOICE_KEY, on ? "on" : "off");
  } catch {
    // Blocked storage: the choice lasts for this page and no longer.
  }
}

/** The one frame the server reads for this (server.py, `kind == "voice"`). */
export function voiceFrame(on: boolean): { type: "voice"; on: boolean } {
  return { type: "voice", on: Boolean(on) };
}
