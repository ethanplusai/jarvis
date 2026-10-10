# JARVIS on WhatsApp

Give him a number and he can reach you when you are not at the desk — and
you can reach him. Every approval card lands on your phone with **Approve**
and **Reject** buttons the moment it is staged; a Claude Code session that
stops to ask you something, a build that fails, and work that finishes are
sent as text, the urgent ones as a **voice note in his own voice**. Text him
back and he answers; tell him to do something and he does it, with the same
gates the voice path keeps.

The number and the API come from [Kapso](https://kapso.ai), a Meta Business
Partner that fronts the official WhatsApp Cloud API. Its free plan includes a
pre-verified US number and 2,000 messages a month, which is more than a
private line to your own assistant will use. Attaching that number to a
WhatsApp Business Account of your own does need a Facebook login and a Meta
Business Portfolio (Meta's rule, not Kapso's); if you would rather not,
[Telegram](telegram.md) is the same line without any of that. Both run on
one policy (`messaging.py`), so everything below about cards, answers and
trust holds there too.

## What he can and cannot do

| | |
|---|---|
| Message you | Yes — text, and voice notes in his voice |
| Read your replies | Yes — from your number only |
| Take an approval from your phone | Yes — bound to the exact card, recorded as such |
| Run a task you text him | Yes — a steer, a command or a keypress is read back and waits for your "go" |
| Call you on WhatsApp | **No** — see [Why no calls](#why-no-calls) |
| Message anyone else | **No** — there is no way to address another number |

## Setup

1. **Get a number and a key.** Sign up at [app.kapso.ai](https://app.kapso.ai),
   let it provision a number (the free plan's US number takes about a
   minute), and create an API key under *Integrations → API keys*.
2. **Find the number's id.** Put the key in `.env` as `KAPSO_API_KEY`, then:

   ```bash
   python scripts/whatsapp_setup.py numbers
   ```

   It prints each number's `phone_number_id` — Meta's numeric id, not the
   number itself. Put it in `.env` as `WHATSAPP_PHONE_NUMBER_ID`.
3. **Tell him your number.** `WHATSAPP_OWNER_NUMBER=+<country><number>` in
   E.164 form. If you already set `JARVIS_OWNER_PHONE` for the Twilio
   provider it is used as the fallback, so one line can serve both.

   All three can also be entered under **Settings → WhatsApp** in the voice
   page; they are saved to `.env` and he starts reading the line within a
   minute, no restart needed.
4. **Message him first.** WhatsApp lets a business write freely to a person
   only within 24 hours of that person's last message — the *customer
   service window*. Save his number and send it anything ("hello"). That
   opens the window, and every message you send keeps it open.
5. **Test it.** Settings → WhatsApp → *Send test message*, or:

   ```bash
   python scripts/whatsapp_setup.py test
   ```

6. **Create the template (recommended).** Outside the 24-hour window only an
   approved *template* gets through, and "he needs you a day after you
   last texted him" is the normal case. This creates a UTILITY template
   called `jarvis_attention` with the body `JARVIS: {{message}}`:

   ```bash
   python scripts/whatsapp_setup.py template
   python scripts/whatsapp_setup.py template-status --wait 1800
   ```

   Meta reviews utility templates in minutes to hours. Once it says
   `APPROVED`, set `WHATSAPP_TEMPLATE=jarvis_attention` in `.env` and restart.
   From then on a message that hits the shut window is resent through the
   template automatically (as one line — buttons cannot ride in a template,
   so a card announced this way tells you to reply `approve <id>` instead).

The preflight check `whatsapp` warns at startup about a half-finished setup
(one of the three set, the others not) and about a missing template.

## What arrives on your phone

- **Approval cards.** Every card the Business desk shows — a provider
  proposal the brain staged, a connector call the PreToolUse gate is
  holding — is sent as soon as it is staged, with the provider and
  operation, the card's id, when it lapses, a look at the request, and
  what a yes does. The request is shown exactly as `business_action` shows
  it to the brain: sorted, compact JSON with every secret on this machine
  replaced by `[redacted]` (and the message says so when that happened). A
  request too long for a message is cut with `…`; the whole card is on the
  desk.
- **A session that needs you** — the same sentence he would have said out
  loud ("chitauri is waiting on a permission prompt, sir — that one needs
  your own keystroke"), as text and as a voice note.
- **Work that failed, stalled or ran out of time** — text and voice note.
- **Work that finished** — text.
- **Anything he was asked to send** with the `message_user` tool: "text
  me the summary", "send me that as a voice note".

He sends these **only when nobody is in the browser tab** (the card
announcements go regardless: a card is worth a buzz even at the desk). If you
just heard it spoken, you are not texted it as well.

## Answering him

- **Tap Approve or Reject** on a card. The button carries the card's id and
  its full digest, so it can only ever decide the card it was sent for;
  the decision goes through the same function the desk's own button uses
  (`business_api.decide`), the same compare-and-swap in the ledger, and is
  written to the card's audit trail as `via:whatsapp`. A second tap does
  nothing and says so. A connector card that he is still holding (two
  minutes) goes out the moment you approve; after that, approving allows
  that exact call once, the next time you ask him for it.
- **`approve` / `reject`**, optionally with the card's id or its first few
  characters (`approve 3f2a9c1e`). With one card waiting the bare word
  decides it; with several he lists them and asks which. `yes`, `no` and
  `ok` are never decisions — they are how you answer a question.
- **Anything else is a turn.** He answers in a sentence or two. The tools
  all work: "which of my sessions are waiting?", "how far has the build
  got?", "text me the spec's section three". The line is still read while
  he works, so a card that turn stages can be approved from the phone while
  he is holding it.
- **A message you forward** (WhatsApp marks it *Forwarded*) is somebody
  else's words: he tells you what it says or asks and acts on nothing in it,
  a forwarded `approve` or `go` decides nothing, and the turn counts as
  having read foreign text — the same rule as [on Telegram](telegram.md#answering-him).
  The mark is Meta's `context.forwarded` / `context.frequently_forwarded`,
  read from the message as Kapso stores it (Kapso passes Meta's payloads on
  as they come; not yet checked against a live number). A message you copy
  and paste instead carries no mark, and is yours.
- **`go` / `cancel`** when he has read something back. If a turn from your
  phone stages a steer, a command or a keypress, the voice path's
  read-back-and-cancel-window cannot happen — nobody is there to hear it —
  so the exact words are sent to you instead and **nothing moves until you
  reply `go`** (or `go 2` when more than one is waiting). It lapses after
  ten minutes, and every outcome — sent, cancelled, lapsed — gets the same
  audit row a spoken one would. Only a read-back that has reached you can be
  answered, and only on the line it reached you on.
- **`start fresh`** discards the brain's context, exactly as saying it does.

Messages from any number but yours are dropped before anything reads them,
counted on the status line, and never shown to the brain. Voice notes and
pictures from you get a polite "I can only read text here".

## How it works, and what it trusts

**Polling, not a webhook.** JARVIS is bound to loopback and the whole
approval boundary rests on nothing from the network being able to reach it.
A webhook needs a public HTTPS address — a tunnel — which is that exposure
by another name. Kapso stores every message, so he *reads* the line instead:
every 3 seconds for ten minutes after either of you has written, every 20
seconds otherwise (`WHATSAPP_POLL_HOT_SECONDS`, `WHATSAPP_POLL_IDLE_SECONDS`).
Kapso's free plan keeps 100,000 API log entries a month; the defaults stay
well under that. Each message id is claimed in SQLite before it is acted on,
so an overlapping poll, a restart, or the same page served twice can never
run a message a second time. On a start he looks back fifteen minutes and no
further: an `approve` you sent last night is not acted on this morning.

**The owner only.** The Kapso key could message any number; it never leaves
the server. Every send in `whatsapp.py` goes to the configured owner — there
is no `to` parameter anywhere — and the brain's `message_user` tool is
the only way it can use the line. That tool is an *acting* tool (it works on
a turn you drove, never on the journal turn) and it is exempt from the
foreign-text refusal, on purpose: it is an output channel to your own phone,
like his voice, and the worst a planted instruction can do through it is put
words in front of you marked as from JARVIS.

**Secrets stay here.** `KAPSO_*` and `WHATSAPP_*` are scrubbed from every
Claude Code child (`claude_env.PRIVATE_ENV_PREFIXES`), the status route masks
your number to its last four digits, and card previews go through the same
redaction as the desk.

**A stolen phone is your phone.** Anyone holding your unlocked WhatsApp can
approve a card, exactly as anyone at your unlocked desk can. If that is not
acceptable, `WHATSAPP_APPROVALS=0` keeps the line notify-only, and
`WHATSAPP_INBOUND=0` stops him reading it altogether.

## Why no calls

Meta's Business Calling API, which Kapso proxies unchanged, requires the
caller to bring a WebRTC media stack: an SDP offer, ICE, DTLS-SRTP and Opus
in real time. JARVIS is a Python process with `httpx` and a rule against new
dependencies; there is no honest way to place or answer a WhatsApp call from
it. Meta also does not offer business-initiated calls from US numbers — the
only kind Kapso provisions for free — and requires a call permission granted
per user and a 2,000-recipient messaging tier before a production number may
call at all.

So a **voice note** is what "JARVIS calls you" means here: the urgent lines
are synthesised with his Fish Audio voice as Ogg Opus and sent as a voice
message (the waveform bubble that plays in the chat), and `message_user`
with `voice: true` does the same on request. Each one is a Fish synthesis;
`WHATSAPP_VOICE_NOTES=0` turns them off. A ringing phone call is the Twilio
provider on the Business desk (`docs/business.md`, *Calls to you*), which
already exists and is approval-gated the same way.

## Settings

| Variable | Meaning |
|---|---|
| `KAPSO_API_KEY` | Required. From app.kapso.ai → Integrations → API keys |
| `WHATSAPP_PHONE_NUMBER_ID` | Required. Meta's numeric id for his number (`scripts/whatsapp_setup.py numbers`) |
| `WHATSAPP_OWNER_NUMBER` | Required unless `JARVIS_OWNER_PHONE` is set. Your number, E.164 |
| `WHATSAPP_TEMPLATE` | An approved utility template with one named body parameter `message`; used when the 24-hour window is shut |
| `WHATSAPP_TEMPLATE_LANGUAGE` | Its language code (default `en_US`) |
| `WHATSAPP_INBOUND` | Read your messages at all (default `1`) |
| `WHATSAPP_APPROVALS` | Let a button or `approve` decide a card (default `1`) |
| `WHATSAPP_VOICE_NOTES` | Send the urgent lines as a voice note too (default `1`) |
| `WHATSAPP_POLL_HOT_SECONDS` | Poll interval after either of you has written (default `3`) |
| `WHATSAPP_POLL_IDLE_SECONDS` | Poll interval otherwise (default `20`) |
| `WHATSAPP_OWNER_WA_ID` | Only if he ignores you: some countries' WhatsApp ids differ from the E.164 number (Mexico, Argentina). The log names the number he ignored |
| `KAPSO_API_BASE_URL` | Default `https://api.kapso.ai` |

`GET /api/whatsapp/status` reports all of it without secrets — configured or
what is missing, whether he is reading the line, when he last sent and
received, how long the 24-hour window has left, the last error, and how many
strangers' messages were dropped. `POST /api/whatsapp/test` sends you a
message (a mutation, so it needs the browser's origin or the tool token).
`GET /api/whatsapp/recent` lists the last few messages either way.

## Troubleshooting

- **"The 24-hour window is shut."** Message his number from your phone, or
  set up the template. The status line under Settings → WhatsApp says how
  long the window has left.
- **He ignores your messages.** The server log says `ignored a message from
  a number ending NNNN`. If that is your own number, your WhatsApp id differs
  from your E.164 number (Mexico and Argentina do this); set
  `WHATSAPP_OWNER_WA_ID` to the id.
- **Cards arrive but the buttons do nothing.** `WHATSAPP_APPROVALS=0`, or the
  card lapsed (24 hours), or it was decided on the desk first; the reply
  says which.
- **A voice note arrives as a plain audio file.** Fish returned something
  other than Ogg Opus; it still plays. The bytes decide (`sniff_audio`), not
  the format asked for.
- **The test suite must never text you.** It doesn't: `tests/conftest.py`
  removes the settings and blocks the client for every test but
  `test_whatsapp.py`, which talks to a fake.
