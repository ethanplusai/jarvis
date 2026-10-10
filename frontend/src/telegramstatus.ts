// What the Settings page says about the Telegram line, as pure functions of
// GET /api/telegram/status, so the wording can be tested without a DOM.
//
// Two rules came out of reviewing the first version:
//
//   * A pairing is watched by its SERIAL and OUTCOME, never by `configured`.
//     `configured` is already true while an owner exists, so pairing a new
//     phone read "Paired" at the first tick while the old phone kept the line.
//   * Nothing here offers an account on the strength of its name. The name on
//     a Telegram account is whatever its owner typed; the page used to list
//     strangers by name with "if that was you, save the id above".

export type PairingOutcome = "" | "paired" | "cancelled" | "lapsed" | "locked";

export interface TelegramStatus {
  configured: boolean;
  touched: boolean;
  missing: string[];
  issue: string | null;
  token_set: boolean;
  owner_id: string;
  bot_username: string;
  // Never the code: the status is a GET. The code is in the POST that made it.
  pairing: { active: boolean; seconds_left: number; serial: number; outcome: PairingOutcome; note: string };
  approvals: boolean;
  voice_notes: boolean;
  polling: boolean;
  last_sent: number | null;
  last_received: number | null;
  last_error: string | null;
  poll_error: string | null;
  ignored_strangers: number;
  recent_strangers: { id: number; at: number }[];
}

/** The line under the Telegram fields: set up or not, paired or waiting,
 * reading the line or not — and what the poll is running into, in EVERY
 * state, because a token Telegram refuses looks exactly like "not paired
 * yet". `poll_error` clears when a poll works; `last_error` is the last
 * failure of any kind. */
export function describeTelegram(s: TelegramStatus, ago: (epoch: number | null) => string): string {
  if (!s.touched) return "Not set up. Optional, and the easier line.";
  if (s.issue) return `Cannot be used: ${s.issue}`;
  if (!s.token_set) return "An owner id is set but there is no bot token.";
  const bot = s.bot_username ? ` (@${s.bot_username})` : "";
  const failing = s.poll_error ? ` The last poll failed: ${s.poll_error}` : "";
  const ignored = s.ignored_strangers
    ? `${s.ignored_strangers} message${s.ignored_strangers === 1 ? "" : "s"} from other accounts ignored.`
    : "";
  if (!s.configured) {
    const parts = [`Bot token saved${bot}, not paired yet: press Pair and send the code from your phone.${failing}`];
    if (ignored) parts.push(`${ignored} A name on an account proves nothing — pair with the code.`);
    return parts.join(" ");
  }
  const parts = [`Paired with id ${s.owner_id}${bot}.`];
  parts.push(s.polling ? "Reading the line." : "Not reading the line yet (restart?).");
  parts.push(`Last sent ${ago(s.last_sent)}; last received ${ago(s.last_received)}.`);
  if (!s.approvals) parts.push("Approvals from the phone are off.");
  if (ignored) parts.push(ignored);
  if (failing) parts.push(failing.trim());
  if (s.last_error && s.last_error !== s.poll_error) parts.push(`Last error: ${s.last_error}`);
  return parts.join(" ");
}

/** Said when Pair is pressed while a phone already has the line: the old
 * phone keeps it until the new one sends the code. */
export const PAIRING_WHILE_PAIRED =
  "Until the new phone sends it, the current one keeps the line. If that phone is lost, press Unpair first.";

/** The pairing prompt in three pieces, so the page can show the code as the
 * one thing to read — large, on a line of its own — rather than six digits
 * buried in a sentence of hint text (measured live: the owner pressed Pair
 * seven times, each press replacing a code he never saw). */
export interface PairingPromptParts {
  lead: string;
  code: string;
  tail: string;
}

export function pairingPromptParts(code: string, botUsername: string,
                                   alreadyPaired: boolean): PairingPromptParts {
  const bot = botUsername ? ` (@${botUsername})` : "";
  return {
    lead: `Open the bot in Telegram on your phone${bot} and send it:`,
    code,
    tail: "(ten minutes, once)" + (alreadyPaired ? ` ${PAIRING_WHILE_PAIRED}` : ""),
  };
}

/** What the page shows once a code is minted, as one line of text: where to
 * send it, and — when a phone already has the line — that it keeps it until
 * the new one sends it. */
export function pairingPrompt(code: string, botUsername: string, alreadyPaired: boolean): string {
  const { lead, tail } = pairingPromptParts(code, botUsername, alreadyPaired);
  return `${lead} ${code}   ${tail}`;
}

/** How pairing code number `serial` ended: "" while it is live, and
 * "lapsed" once a newer code has replaced it. */
export function pairingOutcome(s: TelegramStatus | null, serial: number): PairingOutcome {
  if (!s) return "";
  if (s.pairing.serial !== serial) return "lapsed";
  return s.pairing.outcome;
}

/** What the page says once a code has ended — with the server's note, when
 * there is one (a pairing that holds for this run only). */
export function pairingEndedText(outcome: Exclude<PairingOutcome, "">, note = ""): string {
  switch (outcome) {
    case "paired":
      return note ? `Paired, but ${note}.` : "Paired. He has greeted you on the phone.";
    case "cancelled": return "The code was withdrawn.";
    case "locked":
      return "Too many wrong codes were sent to the bot, so the code was withdrawn. Press Pair for a new one — and if you did not send them, somebody else knows the bot's name.";
    case "lapsed": return "The code lapsed; press Pair again.";
  }
}
