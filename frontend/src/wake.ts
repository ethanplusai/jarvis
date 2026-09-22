/// <reference types="vite/client" />
/**
 * Wake phrase — JARVIS stays dark and silent until he hears it.
 *
 * The phrase comes from VITE_WAKE_PHRASE (frontend/.env.local) and defaults
 * to "hey jarvis". Matching ignores case and punctuation, because the speech
 * recogniser and dictation tools (Wispr Flow) punctuate as they please.
 */

export const DEFAULT_WAKE_PHRASE = "hey jarvis";

export function normalise(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s]/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
}

export interface WakeMatch {
  woke: boolean;
  /** Whatever was said after the phrase, ready to send as the first turn. */
  remainder: string;
}

export function matchWake(text: string, phrase: string): WakeMatch {
  const want = normalise(phrase).split(" ").filter(Boolean);
  if (want.length === 0) return { woke: true, remainder: text.trim() };
  // Walk the ORIGINAL words so the remainder keeps its casing and punctuation.
  const words = [...text.matchAll(/[\p{L}\p{N}]+(?:['’][\p{L}\p{N}]+)*/gu)];
  const norm = words.map((w) => normalise(w[0]).replace(/ /g, ""));
  for (let i = 0; i + want.length <= words.length; i++) {
    if (want.every((w, j) => norm[i + j] === w)) {
      const last = words[i + want.length - 1];
      const cut = (last.index ?? 0) + last[0].length;
      const remainder = text.slice(cut).replace(/^[\s,.;:!?—-]+/u, "").trim();
      return { woke: true, remainder };
    }
  }
  return { woke: false, remainder: "" };
}

export function wakePhrase(): string {
  const configured = (import.meta.env.VITE_WAKE_PHRASE as string | undefined) ?? "";
  return configured.trim() || DEFAULT_WAKE_PHRASE;
}
