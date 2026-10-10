# JARVIS on Telegram

Give him a Telegram bot and he can reach you when you are not at the desk,
and you can reach him. Every approval card lands on your phone with
**Approve** and **Reject** buttons the moment it is staged; a Claude Code
session that stops to ask you something, a build that fails, and work that
finishes are sent as text, the urgent ones as a **voice note in his own
voice**. Text him back and he answers; tell him to do something and he does
it, with the same gates the voice path keeps.

It is the same line as [WhatsApp](whatsapp.md) on a different wire, and the
easier of the two: a bot from @BotFather takes a minute, needs no phone
number, no Facebook login and no Meta business account, has no 24-hour
window and no template review, and its long polling suits a server that is
never reachable from the network. What Telegram cannot do is place a call;
a voice note is what "JARVIS calls you" means here, on either line.

## What he can and cannot do

| | |
|---|---|
| Message you | Yes — text, and voice notes in his voice |
| Read your replies | Yes — from your account only, in the private chat with the bot |
| Take an approval from your phone | Yes — bound to the exact card, recorded as such |
| Run a task you text him | Yes — a steer, a command or a keypress is read back and waits for your "go" |
| Reach you a day later | Yes — no 24-hour window on Telegram |
| Call you | **No** — a bot cannot place calls |
| Message anyone else | **No** — there is no way to address another chat |

## Setup

1. **Make the bot.** In Telegram, open **@BotFather**, send `/newbot`, give
   it a name and a username ending in `bot`. It answers with a token like
   `123456789:AAH…`. That is `TELEGRAM_BOT_TOKEN`; put it in `.env` or in
   **Settings → Telegram** on the voice page.
2. **Pair it with your phone.** Nobody knows their own Telegram id, so the
   bot learns it from you: press **Pair** under Settings → Telegram (or run
   the command below), open the bot on your phone and send it the six-digit
   code. The sender of the right code becomes the owner; `TELEGRAM_OWNER_ID`
   is written to `.env`, and he greets you. The code is good for ten
   minutes, once. Pairing again later moves the line to whichever phone
   sends the new code — that is how a new phone takes over.

   ```bash
   python scripts/telegram_setup.py pair
   ```

   While JARVIS is running the script pairs through the server (two things
   polling one bot fight); with JARVIS stopped it reads the bot itself.
3. **Test it.** Settings → Telegram → *Send test message*, or:

   ```bash
   python scripts/telegram_setup.py test
   ```

That is all. The `telegram` preflight check says when a token is waiting to
be paired.

## What arrives on your phone

- **Approval cards.** Every card the Business desk shows — a provider
  proposal the brain staged, a connector call the PreToolUse gate is
  holding — is sent as soon as it is staged, with the provider and
  operation, the card's id, when it lapses, a look at the request, and what
  a yes does. The request is shown exactly as `business_action` shows it to
  the brain: sorted, compact JSON with every secret on this machine replaced
  by `[redacted]`. Once you decide, the buttons come off the message.
- **A session that needs you** — the same sentence he would have said out
  loud, as text and as a voice note.
- **Work that failed, stalled or ran out of time** — text and voice note.
- **Work that finished** — text.
- **Anything he was asked to send** with the `message_user` tool: "text me
  the summary", "send me that as a voice note".

He sends these **only when nobody is in the browser tab** (the card
announcements go regardless). With both a Telegram and a WhatsApp line set
up, everything goes to both.

## Answering him

- **Tap Approve or Reject** on a card. Telegram gives a button 64 bytes, so
  the tap carries the card's id and the first sixteen characters of its
  digest; the stored card's digest must begin with them, and the decision is
  then made with the full digest through the same function the desk's own
  button uses (`business_api.decide`), recorded in the card's audit trail as
  `via:telegram`. A second tap finds the buttons gone and is told nothing
  changed.
- **`approve` / `reject`**, optionally with the card's id or its first few
  characters (`approve 3f2a9c1e`, or `/approve 3f2a9c1e`). With one card
  waiting the bare word decides it; with several he lists them and asks
  which. `yes`, `no` and `ok` are never decisions.
- **Anything else is a turn.** He answers in a sentence or two. The tools
  all work. The line is still read while he works, so a card that turn
  stages can be approved from the phone while he is holding it.
- **A message you forward** is somebody else's words, and he treats it so:
  he tells you what it says or asks, and acts on nothing in it. A forwarded
  `approve` or `go` decides nothing. The turn counts as having read foreign
  text, exactly as after reading a web page: every tool that changes
  something is refused that turn — runs, builds, steers, commands, notes —
  bar the two that cannot carry anybody's words anywhere (texting you, and
  a single keypress in a dialog, which still waits for your `go`), and he
  writes no memory or record until his context is next tidied. Reading tools
  still work, so "what is this link?" gets an answer. Say what you want done
  in your own words.
- **`go` / `cancel`** when he has read something back: a steer, a command
  or a keypress a turn from your phone staged waits, as text, for your
  explicit `go` (or `go 2` with several waiting), and lapses after ten
  minutes with an audit row either way. Only what has actually reached you
  can be answered, and only on the line it reached you on: until then `go`
  is just a word. If a read-back's send failed as far as he knows, a bare
  `go` asks which you mean; `go 1` still works for what you have seen.
- **`/start`** is a greeting. **`start fresh`** discards the brain's
  context, exactly as saying it does.

Messages from any other account, and anything said in a group, are dropped
before anything reads them, counted on the status line, and never shown to
the brain. Stickers, photos and voice messages from you get a polite "I can
only read text here".

## How it works, and what it trusts

**Long polling, not a webhook.** JARVIS is bound to loopback and the whole
approval boundary rests on nothing from the network being able to reach it.
Telegram's `getUpdates` holds a request open for up to 25 seconds and
answers the moment something arrives, so your message is seen within a
second and an idle line costs one request every 25 seconds — and nothing
listens on any port. A webhook set on the bot elsewhere would make every
poll fail, so he clears it at start. The offset is persisted (per bot, so a
new token starts its own) so a restart carries on where it left off, and
every update id is claimed in SQLite before it is acted on, so an update
redelivered after a crash cannot act twice. A turn runs beside the poll, not
inside it: taps, `approve` and `go` are handled at once, and your messages to
the brain still run one at a time, in order. When JARVIS stops or restarts, a
message still waiting its turn is lost with the process — he tells you once
that he is going offline, so you know to send it again when he is running.

**The owner only.** The bot token could message any chat that has started
the bot; it never leaves the server. Every send in `telegram.py` goes to the
owner's id — there is no `to` parameter — and only a private chat from that
id is read. The brain reaches the phone only through `message_user`, an
acting tool (a turn you drove, never the journal turn) that is exempt from
the foreign-text refusal on purpose: it is an output channel to your own
phone, like his voice.

**Pairing is the trust decision.** Whoever sends the live code, in a private
chat, becomes the owner — in place of any owner there was. The code is shown
only to whoever pressed Pair (the Settings page, or your own terminal) and
never in the status route, which anything on this machine can read; it
lasts ten minutes and works once. While it is live, every six-digit message
you send in a private chat is a pairing attempt, never a message to the
brain; five wrong ones, from anybody, withdraw the code (the page says so;
press Pair for a new one). A message sent before the code was made — it sat
in Telegram's queue before the code existed — is not an attempt at all: it
neither pairs nor counts, and from you it is an ordinary message. "Before"
is judged on Telegram's own clock, read from the `Date` of its answers, with
ten seconds' grace; if the right code still arrives dated too early, the
page says to check this computer's clock. Every other message from anybody but the owner is dropped; the status
keeps the ids of the last few, never the names — a name is whatever its
owner typed.

Somebody who knows your bot's name can spoil a pairing by sending five
wrong codes while one is live. That is the price of the limit; the page
says when it happens, and a new bot from @BotFather has a name nobody knows.

**Unpair** (Settings → Telegram, two clicks, or `POST /api/telegram/unpair`)
forgets the owner at once: nothing is read from that phone or sent to it
until a phone pairs again, and a message of his still waiting its turn is
dropped. If a phone is lost, unpair first and then pair the new one — until
the new phone sends the code, the old one keeps the line.

**Pairing with JARVIS stopped.** `scripts/telegram_setup.py pair` then
reads the bot itself, for pairing only: anything else you send meanwhile is
answered "JARVIS is not running" and decides nothing. If `.env` cannot be
written, a pairing made from the Settings page holds until JARVIS stops (the
page says so); one made by the script with JARVIS stopped ends with the
script, which tells you to put `TELEGRAM_OWNER_ID` in `.env` by hand.

**Secrets stay here.** `TELEGRAM_*` is scrubbed from every Claude Code child
(`claude_env.PRIVATE_ENV_PREFIXES`); the status route never carries the
token; card previews go through the same redaction as the desk. The Bot API
wants the token in every request's URL, and the server logs request URLs, so
every logger that prints one rewrites it first
(`bot123456789:•••` — `telegram.URL_LOGGERS`): the token never reaches
`data/logs`.

**A stolen phone is your phone.** Anyone holding your unlocked Telegram can
approve a card, exactly as anyone at your unlocked desk can.
`TELEGRAM_APPROVALS=0` keeps the line notify-only; **Unpair** takes the line
off it altogether.

## Why no calls

A Telegram bot has no calling API at all, and Meta's WhatsApp calling API
needs a WebRTC media stack and a non-US number (see
[whatsapp.md](whatsapp.md#why-no-calls)). So on both lines the urgent
sentences are synthesised with his Fish Audio voice as Ogg Opus and sent as
a voice message (`sendVoice`, the waveform bubble), and `message_user` with
`voice: true` does the same on request. `TELEGRAM_VOICE_NOTES=0` turns them
off on this line. A ringing phone call is the Twilio provider on the
Business desk (`docs/business.md`, *Calls to you*).

## Settings

| Variable | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Required. From @BotFather |
| `TELEGRAM_OWNER_ID` | Your numeric Telegram id. Filled in by pairing; can be set by hand |
| `TELEGRAM_APPROVALS` | Let a button or `approve` decide a card (default `1`) |
| `TELEGRAM_VOICE_NOTES` | Send the urgent lines as a voice note too (default `1`) |
| `TELEGRAM_API_BASE_URL` | Default `https://api.telegram.org` |

`GET /api/telegram/status` reports all of it without the token — configured,
paired or waiting, the bot's username, whether he is reading the line, when
he last sent and received, the last error and the poll's own error (which
the next good poll clears), the latest code's serial, outcome (`paired`,
`cancelled`, `lapsed`, `locked`) and note — never the code — and the ids of
the last few other accounts that wrote. `POST /api/telegram/pair` checks the token
with Telegram and mints a code, `POST /api/telegram/pair/cancel` withdraws
it, `POST /api/telegram/unpair` forgets the owner, and
`POST /api/telegram/test` sends you a message; all are mutations, so they
need the browser's origin or the tool token. `GET /api/telegram/recent`
lists the last few messages either way.

## Troubleshooting

- **"Not paired yet."** Press Pair and send the code from your phone. If the
  status line also names a failed poll, pairing cannot help: fix that first.
- **"Telegram refused this bot token."** Pair asks Telegram first. Copy the
  token again from @BotFather (`/token`) and save it.
- **"Too many wrong codes were sent to the bot."** Somebody other than you
  sent six-digit guesses while the code was live, so it was withdrawn. Press
  Pair for a new one; if you did not send them, somebody knows your bot's
  name.
- **He never answers.** The status line says whether he is reading the line;
  a `409` in the last error means something else is polling the same bot
  (a second JARVIS, the setup script, or a webhook left by another tool —
  he deletes the webhook himself at start).
- **"Forbidden: bot was blocked by the user".** You blocked the bot in
  Telegram; unblock it and send it anything.
- **A voice note arrives as a plain audio file.** Fish returned something
  other than Ogg Opus; it still plays.
- **The test suite must never text you.** It doesn't: `tests/conftest.py`
  removes the settings and blocks the client for every test but
  `test_telegram.py`, which talks to a fake.
