# ChatGPT stands in while Claude is limited

JARVIS's brain runs on your Claude subscription. When Claude's usage limit is
reached, he used to go quiet until it reset: "I've hit the usage limit until
3 PM, sir." With the fallback switched on, **ChatGPT stands in** — the Codex
CLI, on your ChatGPT subscription — and he goes back to Claude by himself the
first time you speak after the limit resets.

It is off unless you switch it on. While it stands in, what you say, his
memory and whatever his tools return are sent to OpenAI.

## What you notice

- **The switch, once each way.** "Claude's limit is reached until 3 PM, sir.
  ChatGPT is standing in." once ChatGPT has actually answered, and, when the
  limit resets, "Back on Claude, sir." after Claude's first answer. Each
  phone line's next reply carries the same sentence, whichever line the
  switch happened on — "back on Claude" only on a reply Claude gave, and a
  line whose ChatGPT reply failed before it heard is told the limit. A new
  limit that begins before Claude has answered you again is said again;
  the same limit refused again, a minute later, is not.
- **The same JARVIS.** ChatGPT runs on his persona, his memory index and the
  launch prompt the Claude brain has, and is shown the last few things you
  said to Claude, so the conversation carries on.
- **The same tools, through the same gates.** His own tools go through the
  origin, untrusted-content and memory gates at `/internal/tool`; your own
  connected services go through the approval card, exactly as on Claude.
- **Less than Claude has.** No web search, no web fetch, and no shell or file
  access beyond JARVIS's own tools. He says so rather than guessing. Answers
  arrive whole rather than streamed: Codex sends a finished message.
- **Back on Claude, nothing is lost.** Claude's first turn after the limit is
  handed what was said while ChatGPT stood in, marked as a record and not an
  instruction.
- **"Start fresh"** works on either: said while ChatGPT stands in, it clears
  ChatGPT's side at once ("Cleared what I can, sir; the rest goes before
  Claude next answers you") and Claude's before the first turn Claude
  serves. Until then the conversation it discards writes no handover. On
  either brain the fresh conversation starts with no note at all — the
  next ChatGPT thread included — and a "fresh-start" entry in the handover
  journal, written the moment you ask, makes sure no later start, not even
  after JARVIS restarts, picks up a note from before it. If that later
  rotation cannot be done, he says so. On a phone line the reply is the
  line that actually applies, and the room is not told.
- **Both limited.** "Claude and ChatGPT have both hit their limits, sir:
  Claude until 3 PM, ChatGPT until 5 PM."
- **A limit in the middle of a turn.** When the limit lands after a turn has
  already used a tool, the turn is not run again on ChatGPT; he says the
  limit and what the turn had done ("…I had already sent create_post on
  linkedin"). The limit is said once, by the turn it ended.
- **ChatGPT's first try fails.** He still says Claude's limit, and that
  ChatGPT couldn't answer either.

## Setup

1. **Install Codex.** Codex Desktop is enough (JARVIS finds the CLI it keeps
   under `%LOCALAPPDATA%\OpenAI\Codex\bin`), or `npm install -g @openai/codex`.
2. **Sign Codex in for JARVIS**, once:

   ```
   python scripts/chatgpt_setup.py login
   ```

   This runs Codex's own `codex login` in your terminal, with JARVIS's Codex
   home. Choose **Sign in with ChatGPT**; the sign-in happens in your
   browser and JARVIS never sees it. (`--device-auth` signs in with a code
   on another device instead.) An API-key login is refused: it would bill the
   key, not your subscription.
3. **Switch it on:** `JARVIS_CHATGPT_FALLBACK=1` in `.env`, then restart
   JARVIS.
4. **Check it:** `python scripts/chatgpt_setup.py status`. The same check runs
   at every start (preflight's `chatgpt_fallback`, with the other start-up
   checks in the log), so a fallback that could not stand in shows up then,
   not in the middle of a conversation.

`logout` signs JARVIS's Codex out again. Your own `~/.codex` is never
touched: JARVIS's Codex lives in `data/codex-home` (`<JARVIS_DATA_DIR>/codex-home`),
so your AGENTS.md, skills and plugins stay out of his prompts, and his
threads stay out of Codex Desktop's history. It runs in an empty folder of
its own, outside any repository (`%LOCALAPPDATA%\JARVIS\codex-cwd` when the
data directory is inside JARVIS's checkout, as it is by default).

JARVIS must be listening on loopback (`127.0.0.1`, or all interfaces with
`0.0.0.0`/`::`) for your connected services to be reachable on ChatGPT: the
gateway sends JARVIS's token to the gate, and only ever over loopback.
Bound to one LAN address, the fallback still answers with JARVIS's own
tools, and the start-up check says the connections are left out.

| Variable | Default | |
|---|---|---|
| `JARVIS_CHATGPT_FALLBACK` | off | `1` switches the fallback on |
| `JARVIS_CHATGPT_MODEL` | `gpt-5.5` | the model; a "code mode" model is refused |
| `JARVIS_CODEX_PATH` | found | a `codex.exe` to use instead of the one found |

## How it is kept safe

The fallback is a second brain with **the same gates as the first**, and
where a gate could not be kept the capability was removed rather than
approximated.

- **Codex has no tool of its own.** Every feature that is a tool, reaches a
  service or acts on the machine is switched off on the command line —
  shell, web search, image generation, apps and plugins, browser and
  computer use, sub-agents, memories, hooks — along with the sandbox set to
  read-only, approvals set to never, and `--ignore-user-config`,
  `--ignore-rules` and `--strict-config`. Where the conversation goes (the
  provider and ChatGPT's address) and any extra instructions are pinned to
  Codex's own defaults on every run, so no configuration can move them.
  Each flag was measured against codex-cli 0.155.0-alpha.9.2 with a fake
  model server. What remains is JARVIS's MCP server and the three MCP
  resource readers, which reach only those servers.
- **An update cannot quietly add one.** Before standing in, JARVIS asks Codex
  what is switched on (`codex features list`, read strictly: every line must
  be understood and every switched-off feature seen off) and refuses if
  anything is on that he has not vetted, or if a feature he switches off no
  longer exists (a rename would otherwise stay on). It refuses a "code mode"
  model, which carries a JavaScript tool with the shell inside it, by both
  the catalog Codex renders now (`codex debug models`) and the one its last
  run refreshed. A binary updated in place is vetted again.
- **And what Codex does is watched.** A turn that reports anything but an
  answer, reasoning or an MCP call — a shell command, a web search, a file
  change — or that calls a service JARVIS did not give it, is killed at
  once, what it touched is treated as untrusted, he says so, and ChatGPT is
  not used again until JARVIS restarts. A call to a service JARVIS did not
  give it is named, with a warning that it may have gone through (Codex
  reports a call as it starts it), and so is what the turn had already
  done. Codex's own resource readers are not a breach: the model is told
  to prefer them to a web search it does not have, and they reach nothing
  but the services JARVIS gave it (measured) — only one answered by some
  other service is, and a tool of the same name on some other service is
  that service's tool. A Codex that refuses one of JARVIS's settings, or a
  model its own run now marks code-mode, is not used again for the rest of
  the limit. A turn already waiting when that happens does not run either.
- **Nothing configures it behind his back.** A machine-wide Codex
  configuration (`config.toml`, `managed_config.toml` or `requirements.toml`
  under `%ProgramData%\OpenAI\Codex`, found where Codex itself looks, which
  `--ignore-user-config` does not drop) rules the fallback out, checked
  before anything else, again right before every turn, and after it: one
  that appeared while ChatGPT was answering — even one gone again, since
  the folder changed — sets that answer aside and turns ChatGPT off until
  JARVIS restarts, and he says why whenever he is asked afterwards. He says it is this computer's
  Codex configuration and names the file: no update of JARVIS gets past
  it, and once the file is gone the fallback is available again.
- **Checked against the real Codex.** The strict reading of `codex features
  list` passes the real table from codex-cli 0.155.0-alpha.9.2 with
  JARVIS's flags (kept as a test fixture). In that build `unified_exec`
  reads as on whatever is passed; it only chooses how the shell tool is
  built, and the shell tool itself is off (measured: switching it off
  removes `exec_command`), so it is allowed only while that holds.
- **Your connected services are behind the approval card.** Codex is not
  given your servers; it is given `guarded_mcp.py` in front of each one,
  which puts every tool call to `/internal/pretool` — the gate the Claude
  path's hook uses — and forwards only an explicit allow. Everything else a
  server could do without a tool call (resources, prompts, asking the model
  anything) is refused on the wire.
- **What a service returns taints the turn before it arrives.** The gate
  marks the turn as having read somebody else's words at the moment it lets
  a call through, keyed to the turn by a secret only that turn's gateway
  and JARVIS server hold. A call from a turn that has ended is refused, an
  approval is never spent on one, and a call without the secret during a
  ChatGPT turn — the idle Claude process beside it, woken by another
  session — is not taken as yours. What that process reads, beside a
  ChatGPT turn or between turns, through JARVIS's tools or your services,
  counts against Claude, not against the ChatGPT turn — and against a
  Claude turn that began while it read, since Claude has one conversation;
  being woken by another session at all counts as having read something.
  A message from another session that Claude is still answering when you
  speak is told apart from your turn exactly: see "Telling a wake from a
  turn".
  What a ChatGPT turn's call brings back after the turn has ended counts
  against that turn's own thread, not the one that started since; and an
  action whose turn ended while it waited is refused.
- **One name for one tool.** Tool names reach the gate exactly as the Claude
  CLI writes them (anything but letters, digits, `_` and `-` becomes `_`),
  so one policy and one approval cover both brains. The gate also reads
  the tool's own name, so `get&delete` or `search+reply` still counts as
  two verbs, while `list.issues` stays one: on ChatGPT the gateway sends
  it, and on Claude JARVIS asks the Claude CLI what each connected service
  calls its tools. A Claude tool with `_` in its name that the CLI has not
  reported yet is held for your approval rather than guessed at. What a
  turn reports it had done agrees with the gate. A connection whose name
  would split somewhere else (`jarvis_`, `jarvis.`, `a..b`), or that would
  be written the same as another (`a.b` and `a_b`), is refused on both.
- **ChatGPT's thread has its own taint.** What it has read stays with it
  until a new thread starts (a new limit, a new Claude generation, "start
  fresh"), and a memory write is refused on it exactly as on Claude. When
  Claude takes over, what ChatGPT said is handed over walled as untrusted
  model output, and Claude's generation inherits the thread's taint with it.
- **Nothing leaks into Codex's environment.** It runs with the environment a
  Claude Code child gets, minus every `OPENAI_*` and `CODEX_*` variable (one
  of them bills an API key, others redirect Codex), in an empty private
  folder, and is killed — with every server it started — when a turn runs
  out of time or JARVIS stops.
- **Codex never gives up on a call first.** Its own per-call limit is longer
  than the turn's ceiling, whatever that is set to, so an approved post
  that takes three minutes is not reported to the model as failed while it
  still goes out. The turn's own ceiling decides, and a turn it ends says
  what it had reached for.
- **Only you.** A turn falls back only when YOU are talking. The journal,
  the warm-up and every other turn JARVIS starts himself are Claude's or
  nobody's.
- **A turn is never run twice.** If Claude hits the limit partway through a
  turn that had already acted or spoken, JARVIS reports what it did rather
  than running it again on ChatGPT.

## When Claude's process is down too

Claude refuses the warm-up that starts a new brain process while its limit
holds. That used to count as a crash, and three in five minutes retired the
brain for good. A warm-up refused while the limit holds now waits for the
reset instead — a refusal counts as the limit for at least a minute, even
when the reset time it reports has already passed on this machine's clock,
or when the CLI reports it only in words. Only the CLI's own words count,
as the CLI itself words a limit: not a capacity refusal it says is "not
your usage limit", not fast mode's own limit, and never anything the model
wrote. A reset time more than a week away is not believed.
With the fallback on, ChatGPT answers in the meantime; with it off, he says
"I've hit the usage limit until …" rather than "still starting". No
rotation is attempted while the limit holds.

## Telling a wake from a turn

Claude's process accepts messages from your other Claude sessions, and each
one starts a turn of its own with nothing from JARVIS — a wake. Its words
and yours come out of the same process. JARVIS used to take whatever the
process said while your turn was in flight for your turn's, so a wake
still running when you spoke had its answer spoken to you, its end taken
for the end of your turn, and its tool calls made as if you had asked —
and your own message was then answered to nobody. (It predates the
fallback. Guessing a wake's end from silence was tried three times and
broke three ways: a wake silent before its first word, one queued behind
another, and your message queued behind a wake.)

Now each message JARVIS sends Claude carries a fresh tag, and Claude's CLI
echoes it back as the message enters its conversation. Until your
message's echo, whatever the process says is somebody else's: you do not
hear it, it is not counted as your turn's, its tool calls act as nobody's
(an action is refused, an approval is never spent), and the end of its
turn is not the end of yours. From the echo on, it is your turn's. A wake
that ran while your message waited — or one your message was answered
inside of, or one that was folded into your turn — counts as something
Claude has read for your turn, so it will not act on it without being
asked again. A wake busy with a tool does not get your turn killed for
the quiet; your turn's overall limit still applies.

What the Claude CLI does, measured on version 2.1.270 — first read out of
its bundle, then run against a stand-in for Anthropic's API on this
machine, with no account and no usage:

- With `--replay-user-messages`, every message is echoed as a `user` event
  with `isReplay: true` and the `uuid` the message was sent with. The echo
  comes just before the turn's first output, after its `system` events,
  and the turn's `result` names the messages it answered
  (`user_message_uuid`, `user_message_uuids`). A turn that fails before
  any output is never echoed, but its `result` names it all the same, and
  that ends it (read in the bundle; a stand-in API cannot make the CLI
  fail that early — everything else here was also seen running).
- A message sent while another turn runs waits for that turn's `result`,
  or is folded into it at its next tool boundary and echoed there, in the
  middle of the other turn's stream. One `result` then ends both.
- A message from another session is echoed too, with `isSynthetic` and
  `origin.kind: "peer"`; its turn's `result` names no message of JARVIS's.
  It is never merged with one of JARVIS's into a single turn, but it can
  be folded into JARVIS's turn at a tool boundary.
- A tag the CLI has already seen is acknowledged and not run again, so
  every message gets a new one.

The tests drive all of this through the real reader against a stand-in
CLI that queues, folds and echoes the same way
(`tests/test_wake_echo.py`). `python scripts/probe_cli_echo.py` checks
each point above against whichever `claude` is installed, the same way the
measurement was made; run it after upgrading Claude Code.

## Not in this version

- Streaming: Codex sends each message whole, so a long answer is heard when
  it is finished.
- A restart during a limit: what ChatGPT said is handed to Claude in memory,
  so if JARVIS is restarted before Claude comes back, the next start does not
  hear it (the handover journal is Claude's own note, and Claude is limited
  when it would be written).
- A live end-to-end run on your account: every test uses a stand-in Codex
  and never reaches OpenAI. The first real turn is the first time your
  ChatGPT account is used.
