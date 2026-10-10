/**
 * Who answers a session's prompts, and where — in the dashboard's words.
 *
 * `origin` is session_watch.py's reading of the roster's `entrypoint`, from a
 * closed set: terminal, desktop, editor, remote, background (a PROGRAM started
 * it — the Agent SDK, `claude -p`, an MCP client — and answers its prompts
 * over stdio), or other. This used to be one badge for every blocked session,
 * "your keystroke / it wants a key pressed in that terminal", and it was on a
 * session Paperclip was driving through the SDK, which had no terminal and
 * whose prompts Paperclip answered itself in under three seconds. The server
 * now keeps a program's prompts off the user until the program has sat on one
 * past a minute; when one reaches this page, it says whose it is.
 *
 * Pure — no DOM — so it is tested with the node runner (test/promptowner.test.ts).
 */

export interface Hand {
  /** The short badge beside the reason. */
  pill: string;
  /** "bad" is the loud red kept for what the USER must do; a program's
   * prompt is a warning about that program, not an instruction to the user. */
  tone: "bad" | "warn";
  /** One sentence: where it is, and who can answer it. */
  note: string;
}

const HANDS: Record<string, Hand> = {
  terminal: {
    pill: "your keystroke",
    tone: "bad",
    note: "JARVIS cannot answer this one — it wants a key pressed in its terminal.",
  },
  desktop: {
    pill: "in the desktop app",
    tone: "bad",
    note: "It is waiting in the Claude desktop app — answer it there. JARVIS cannot.",
  },
  editor: {
    pill: "in your editor",
    tone: "bad",
    note: "It is waiting in your editor — answer it there. JARVIS cannot.",
  },
  remote: {
    pill: "in the Claude app",
    tone: "bad",
    note: "It is waiting in the Claude app it was started from — answer it there. "
        + "JARVIS cannot.",
  },
  background: {
    pill: "its program's",
    tone: "warn",
    note: "The program that started this session shows that prompt, and has held "
        + "it past the minute a program normally takes. Only that program can "
        + "answer it — there is no terminal, and JARVIS cannot.",
  },
};

const UNPLACED: Hand = {
  pill: "your hand",
  tone: "bad",
  note: "It wants a person, wherever it was started. JARVIS cannot answer it.",
};

/** Who must answer a prompt the inbox socket cannot, by origin. An origin
 * outside the closed set — an older server, or anything else — gets the one
 * reading that claims nothing about a terminal. */
export function handFor(origin: string): Hand {
  return Object.prototype.hasOwnProperty.call(HANDS, origin) ? HANDS[origin] : UNPLACED;
}

/** The band for a `working` session paused on its host program. `reason` is
 * the roster's `waitingFor`, verbatim — this goes to textContent — or "" when
 * the roster named none, and then no reason is invented. */
export function hostWaitLine(reason: string): string {
  if (!reason) {
    return "It is paused on the program that started it, which answers for it. "
         + "Nothing is asked of you.";
  }
  return `It is paused on “${reason}”, which the program that started it answers `
       + "itself. Nothing is asked of you.";
}

/** "started as": JARVIS's reading, then the roster's own word for it, so a
 * reader can check the one against the other. */
export function startedAs(origin: string, entrypoint: string | undefined): string {
  return entrypoint ? `${origin} · ${entrypoint}` : origin;
}
