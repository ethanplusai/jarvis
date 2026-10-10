import { test } from "node:test";
import assert from "node:assert/strict";
import {
  describeTelegram, pairingEndedText, pairingOutcome, pairingPrompt, pairingPromptParts,
  PAIRING_WHILE_PAIRED, type TelegramStatus,
} from "../src/telegramstatus.ts";

const never = (epoch: number | null) => (epoch ? "earlier" : "never");

function status(overrides: Partial<TelegramStatus> = {}): TelegramStatus {
  return {
    configured: true, touched: true, missing: [], issue: null, token_set: true,
    owner_id: "424242001", bot_username: "jarvis_test_bot",
    pairing: { active: false, seconds_left: 0, serial: 0, outcome: "", note: "" },
    approvals: true, voice_notes: true, polling: true,
    last_sent: null, last_received: null, last_error: null, poll_error: null,
    ignored_strangers: 0, recent_strangers: [],
    ...overrides,
  };
}

test("a pairing is done when ITS outcome says so, not when an owner exists", () => {
  const live = status({ pairing: { active: true, seconds_left: 500, serial: 3, outcome: "", note: "" } });
  assert.equal(live.configured, true, "already paired with the old phone");
  assert.equal(pairingOutcome(live, 3), "", "still waiting for the new one");
  const done = status({ pairing: { active: false, seconds_left: 0, serial: 3, outcome: "paired", note: "" } });
  assert.equal(pairingOutcome(done, 3), "paired");
});

test("a code replaced by a newer one has lapsed for whoever was watching it", () => {
  const newer = status({ pairing: { active: true, seconds_left: 600, serial: 4, outcome: "", note: "" } });
  assert.equal(pairingOutcome(newer, 3), "lapsed");
  assert.equal(pairingOutcome(null, 3), "");
});

test("every ending says what happened", () => {
  assert.match(pairingEndedText("paired"), /Paired/);
  assert.match(pairingEndedText("cancelled"), /withdrawn/);
  assert.match(pairingEndedText("lapsed"), /lapsed/);
  assert.match(pairingEndedText("locked"), /wrong codes/);
});

test("a pairing that holds for this run only says so", () => {
  const text = pairingEndedText("paired", "paired for this run only; put TELEGRAM_OWNER_ID=1 in .env");
  assert.match(text, /this run only/);
  assert.doesNotMatch(text, /greeted/);
});

test("pairing while paired says the old phone keeps the line until then", () => {
  const replacing = pairingPrompt("123456", "jarvis_test_bot", true);
  assert.match(replacing, /send it: 123456/);
  assert.match(replacing, /\(@jarvis_test_bot\)/);
  assert.ok(replacing.includes(PAIRING_WHILE_PAIRED));
  assert.match(replacing, /current one keeps the line/);
  assert.match(replacing, /Unpair/);
  const first = pairingPrompt("654321", "", false);
  assert.match(first, /send it: 654321/);
  assert.ok(!first.includes(PAIRING_WHILE_PAIRED), "nothing to keep on a first pairing");
});

test("the code stands on its own, apart from the words around it", () => {
  const parts = pairingPromptParts("482915", "jarvis_test_bot", false);
  assert.equal(parts.code, "482915", "the page renders exactly this, large, on its own line");
  assert.doesNotMatch(parts.lead, /\d{6}/, "the digits are not buried in the sentence");
  assert.doesNotMatch(parts.tail, /\d{6}/);
  assert.match(parts.lead, /\(@jarvis_test_bot\)/);
  assert.match(parts.tail, /ten minutes, once/);
  assert.ok(!parts.tail.includes(PAIRING_WHILE_PAIRED));
  const replacing = pairingPromptParts("123456", "", true);
  assert.ok(replacing.tail.includes(PAIRING_WHILE_PAIRED), "replacing a phone still says who keeps the line");
  assert.doesNotMatch(replacing.lead, /\(@/, "no bot name, no empty brackets");
});

test("the one-line prompt is the parts, in order", () => {
  const parts = pairingPromptParts("123456", "jarvis_test_bot", true);
  const line = pairingPrompt("123456", "jarvis_test_bot", true);
  assert.equal(line, `${parts.lead} ${parts.code}   ${parts.tail}`);
  assert.ok(line.indexOf(parts.lead) < line.indexOf(parts.code));
  assert.ok(line.indexOf(parts.code) < line.indexOf(parts.tail));
});

test("a failing poll is named while unpaired too", () => {
  const text = describeTelegram(status({
    configured: false, owner_id: "", missing: ["TELEGRAM_OWNER_ID"],
    poll_error: "Telegram returned HTTP 401 (error 401) — Unauthorized",
  }), never);
  assert.match(text, /not paired yet/);
  assert.match(text, /The last poll failed: Telegram returned HTTP 401/);
});

test("an old send failure is not called a failing poll", () => {
  const text = describeTelegram(status({
    configured: false, owner_id: "", last_error: "Telegram returned HTTP 403 (error 403)",
  }), never);
  assert.doesNotMatch(text, /poll failed/);
});

test("strangers are counted, never offered by name", () => {
  const text = describeTelegram(status({
    configured: false, owner_id: "", ignored_strangers: 3,
    recent_strangers: [{ id: 777000777, at: 1 }],
  }), never);
  assert.match(text, /3 messages from other accounts ignored/);
  assert.doesNotMatch(text, /save the id/);
  assert.doesNotMatch(text, /777000777/);
});

test("a paired line reports what it is doing, and a poll error once", () => {
  const conflict = "Telegram returned HTTP 409 (error 409)";
  const text = describeTelegram(status({ approvals: false, last_error: conflict, poll_error: conflict }), never);
  assert.match(text, /Paired with id 424242001 \(@jarvis_test_bot\)/);
  assert.match(text, /Reading the line/);
  assert.match(text, /Approvals from the phone are off/);
  assert.equal(text.split(conflict).length - 1, 1, "said once, not twice");
  const sendOnly = describeTelegram(status({ last_error: "Telegram returned HTTP 403 (error 403)" }), never);
  assert.match(sendOnly, /Last error: Telegram returned HTTP 403/);
});

test("the states before a token", () => {
  assert.match(describeTelegram(status({ touched: false }), never), /Not set up/);
  assert.match(describeTelegram(status({ issue: "TELEGRAM_BOT_TOKEN does not look like a bot token" }), never), /Cannot be used/);
  assert.match(describeTelegram(status({ token_set: false }), never), /no bot token/);
});
