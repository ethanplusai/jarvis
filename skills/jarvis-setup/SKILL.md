---
name: jarvis-setup
description: Use when helping someone install, configure, or debug a fresh clone of JARVIS (this repo) — especially "the mic doesn't work", "JARVIS says his language systems are down", any Firefox/browser question, login/auth failures, or Accessibility permission prompts. Carries facts about this project that were only learned by hitting them live; check it before guessing.
---

# Setting up JARVIS

Everything below was learned the hard way, in a real setup or a real live
session, not guessed. Check a claim against the code cited before repeating
it to a user — if something here stops being true, the citation is exactly
what lets you find out.

## The microphone only works in Google Chrome

This is a hard constraint, not a preference. The frontend uses the
`SpeechRecognition` / `webkitSpeechRecognition` Web Speech API
(`frontend/src/voice.ts`) to transcribe the user's voice in the browser.
Firefox has never implemented that API at all — there is no flag, no
polyfill, no workaround. Safari's support is too inconsistent to rely on.
If someone asks to use Firefox (or anything but Chrome) for the mic, tell
them directly that it will not work, rather than letting them debug a
"broken" mic for an hour. `README.md` already states this requirement; don't
let a setup walkthrough contradict it.

## He listens only while Space is held, unless told otherwise

Hold-to-talk is the default listening mode (`frontend/src/listenmode.ts`).
Measured live on 2026-09-24 with the microphone always open: JARVIS
transcribed another assistant's spoken report from the same room, took it
as a barge-in mid-sentence and ran a whole turn on it — echo suppression
knows his own voice and nobody else's. So "he doesn't hear me" on a fresh
install usually means the user is not holding Space (the idle status line
says "hold Space to talk"), not a broken microphone. Space does nothing
while a text box, select or button has focus. The always-open microphone
is Settings → Listening → "Microphone always open", saved per browser.
M mutes the microphone, V mutes his voice, Esc stops him.

## Chrome's microphone permission is scoped per origin — INCLUDING THE PORT

`http://localhost:5173` and `http://localhost:5174` are different origins as
far as the mic grant is concerned. Moving the frontend to a different port —
restarting Vite after a port conflict, following an old bookmark, `--port`
on the backend changing the URL you open — silently loses the grant, and
Chrome does **not** re-prompt once a permission has been dismissed; it just
stays denied with no visible error. This has cost a live session before.
If the mic stops working after everything
else looks fine, the first question is "did the URL's port change" — check
`chrome://settings/content/microphone` for the exact origin in use, not just
"is the mic allowed somewhere."

## JARVIS runs on the Claude Code subscription — never an API key

The voice brain (`brain.py`) is one long-lived `claude -p` process
authenticated by the user's `claude` login, not the Anthropic API.
`claude_env.child_env()` strips every `ANTHROPIC_*` variable from the
environment handed to every spawned Claude Code child — brain and run
pipeline alike — because the CLI silently *prefers* an inherited
`ANTHROPIC_API_KEY` over the logged-in session, without saying so
(`claude auth status` still reports `loggedIn: true` while billing moves to
the key). It strips every `CLAUDE_*`, `MCP_*` and `OTEL_*` variable too
(keeping `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_GIT_BASH_PATH`), because a
JARVIS started from inside a Claude Code session inherits that session's
settings and the CLI acts on them — `CLAUDE_CODE_EFFORT_LEVEL` overrides the
brain's `--effort`. Start JARVIS with `scripts/start-jarvis.ps1`, which
starts it without them; preflight's `claude_session_env` says when it was
started inside a session some other way. So:

- Putting `ANTHROPIC_API_KEY` in `.env` is not a setup step and does nothing
  useful — it is a leftover from a different project's instructions, or a
  misunderstanding, and is worth flagging if you see it.
- Setup is exactly: install Claude Code (`npm install -g
  @anthropic-ai/claude-code`, 2.1.224 or newer) and run `claude` once to log
  in with a Claude subscription. No key, anywhere.

## An expired login sounds like a JARVIS problem, not an auth problem

When the CLI's OAuth session can't refresh, JARVIS's voice brain fails and
says only **"my language systems are down"** — nothing more diagnostic than
that reaches the user by voice. The actual error
(`OAuth session expired and could not be refreshed`) is in the server log,
not in anything spoken. If a fresh install "won't talk," check the log
before anything else.

The check itself is: is `claude` actually logged in, **in the config
directory JARVIS's process will actually use**. `CLAUDE_CONFIG_DIR` can
point somewhere other than the default `~/.claude`, and this has produced a
real false negative before: a debugging session inherited
`CLAUDE_CONFIG_DIR=~/.claude-orcha` (which had a valid login), while the
Terminal-launched server used the default `~/.claude` (which did not) — every
test passed, and the live server still failed. Confirm which directory is in
play (`echo $CLAUDE_CONFIG_DIR`, defaulting to `~/.claude` if unset) and run
`claude auth status` under *that exact* environment, not whatever shell you
happen to be debugging from. `preflight.py`'s `claude_login` check does this
correctly and names the config directory it checked in its message — read
that output rather than re-deriving it by hand.

## Accessibility permission is granted to the app that LAUNCHES JARVIS

`dialog.py` sends a keystroke to a Claude Code session's Terminal window via
System Events, which requires macOS Accessibility (assistive access). macOS
attributes that permission to **the app that launched the `python` process**
— almost always Terminal.app — not to `python` or `osascript` themselves.
So: grant Terminal.app (or whichever app started the server) under System
Settings → Privacy & Security → Accessibility, not "python." If Terminal.app
is already ticked and it still fails, check whether it's actually running
from a randomised `/private/var/.../AppTranslocation/` path (macOS does this
to apps launched straight from Downloads; `ps -o comm= -p <pid>`) — a grant
does not follow the app there. Move it to `/Applications` and relaunch.

`preflight.py`'s `accessibility` check detects the failure (AppleScript
error `-1728` / "not allowed assistive access") without ever triggering the
permission prompt itself, and its remedy text carries this same guidance.

## "Waiting on a permission prompt" — and there is no window to find

JARVIS watches every Claude Code session on the machine, including ones a
PROGRAM started: Paperclip, anything built on the Agent SDK, a `claude -p`.
Those take their permission prompts to the program over stdio
(`--permission-prompt-tool stdio`), and the program answers them itself —
Paperclip's default policy is `approve-all`, measured at 0.06–2.34 s a
prompt. There is no terminal and nothing for the user to press.

Before 2026-09-28 JARVIS could not tell those sessions apart: every roster
`entrypoint` it did not know (`sdk-ts` among them) was read as a terminal,
so each of those sub-second waits was announced as "that one needs your own
keystroke", six times in seven minutes, and the user went looking for a
window that did not exist. `session_watch.py` now reads the CLI's full list
of entrypoints: a program's session stays `working` while its program
answers (the dashboard says "paused on … which the program that started it
answers"), and is announced only if the program has sat on a prompt for
`HOST_ANSWER_GRACE_SEC` (60 s) — as the program's prompt, never as a
keystroke. Every announcement now says where a prompt is: its terminal, the
Claude desktop app, the editor, or the program that started it.

To see who started a session: the dashboard's Sessions tab shows "started
as" (e.g. `background · sdk-ts`), and the roster file itself is
`~/.claude/sessions/<pid>.json` — read its `entrypoint`. If a genuinely new
entrypoint appears in a future CLI it reads as `other`: still announced at
once (nothing is hidden), but with no claim that it has a terminal. Add it
to `_ORIGIN_BY_ENTRYPOINT`; tests/test_prompt_owner.py lists the CLI's own
table, taken from the binary.

## `FISH_API_KEY` is genuinely required — there is no fallback voice

`tts.py` calls the Fish Audio API directly; if the key is missing or empty,
`synthesize_chunk` simply returns `None` — no error, no local TTS, no
built-in voice of any kind. Without a real key from
[fish.audio](https://fish.audio/), JARVIS has no voice at all, which reads
to a new user as "it's just broken." This is the one piece of setup that
cannot be skipped or worked around.

## Connecting a service: one file, and it is not the one you'd guess

JARVIS ships connected to nothing — no calendar, no mail, no notes. He
connects to whatever the user brings, through MCP, and the whole of that is
one file:

```
<JARVIS_DATA_DIR>/jarvis/connections.json     # default: ./data/jarvis/connections.json
```

Not the repo, not `~/.claude.json`, not `.mcp.json`. It is seeded on first
start by `data_paths.sync_connections()` with an empty `mcpServers` block and
notes explaining itself. One entry looks exactly like the block in any MCP
server's README:

```json
{
  "mcpServers": {
    "notion": {
      "command": "npx",
      "args": ["-y", "@notionhq/notion-mcp-server"],
      "env": { "NOTION_TOKEN": "secret_..." }
    }
  }
}
```

A URL server is the same shape: `{"type": "http", "url": "https://..."}`.
Then **restart `server.py`** — the config is generated at boot
(`server._write_mcp_config`) and nothing re-reads it while JARVIS is running.

**Confirm it by asking him: "what are you connected to?"** That runs the
`connections` tool, which answers from what the CLI actually started — the
servers running, the tools each is offering, and anything that would not
start. It is a real check, not a recitation, so it is also the fastest way to
find out that it did *not* work.

### The user's other MCP servers are deliberately invisible

`brain.py` passes `--strict-mcp-config`, so the servers in `~/.claude.json`,
in a project's `.mcp.json`, and every Claude Desktop connector are ignored —
on purpose. Adopting one into JARVIS is meant to be a deliberate act, not
something he inherits because it was sitting in a config file. If someone
says "but it works in Claude Code", that is why: it has to be in
`connections.json` as well.

### When nothing happens

In order of likelihood, and every one of these is *said out loud* by
`connections` and logged at startup by `_write_mcp_config` — check there
before guessing:

- **The file does not parse.** A trailing comma. Everything in it is dropped.
- **The `mcpServers` wrapper is missing** — the inner half of a README's
  snippet pasted straight in.
- **The server would not start**: wrong command, missing `npx`, a token the
  server rejects. The CLI reports this as `"status": "failed"` in its init
  event and `connections` names it.
- **The server started too late.** `npx`, `uvx`, `uv run` and `pipx run`
  resolve packages on every launch, and install first whenever a dependency
  has released. The server then joins after the brain has recorded its
  roster: `brain: MCP roster incomplete: missing=<name>` in
  `data/logs/backend.err.log`, and the `mcp_launchers` preflight check warns
  at startup. Install the server once (a venv, or `npm install` into its own
  folder) and point `command` at the installed executable. Measured on this
  install: 22 s through `uvx` against 1.2 s from a venv.
- **The name.** Tools arrive as `mcp__<server>__<tool>`, so a name with a
  space, a slash or a `__` in it is refused. So is `jarvis`, which is his own.
- **He was not restarted.**

### Every tool costs context on every turn

Measured against `claude` 2.1.259 with JARVIS's exact flag set: about **250
tokens per tool**, resident in every single turn — a twelve-tool server is
~3,300, and JARVIS's own thirty-one are ~7,600. Five servers is real money on
a subscription and real latency on every reply. `connections` says the figure
out loud. It is not counted as *memory* — `brain.py` measures the resident
floor on the warm-up turn and the rotation budget is spent on the
conversation, not the tool schemas — except on a model whose window is too
small to hold the floor plus the budget, where the budget is capped and every
token of floor is a token less conversation (see "Context rotation" in the
README). "Connect what you will use" is still the advice.

### Whatever a connected server returns is treated as untrusted

Like a web page, and for the same reason: the user vouched for the server's
code, not for the Notion page somebody shared with them or the issue a
stranger opened. `brain.untrusted_tool_source` marks the turn, and
`server._untrusted_content_refusal` shuts the acting tools for the rest of
it. It is not only the web: `server.TAINTING_TOOLS` names every reader that
marks a turn — repository files, other sessions' transcripts, run output,
documents, the user's own screen — and everything that acts is shut, bar
`answer_dialog` (one keystroke) and the tools that only fetch more to read.
If someone reports "JARVIS refused to start a run right after reading my
Jira ticket", or right after reading a README — that is this, working.
Asking again re-opens it.

## Giving him your phone (optional): Telegram first

The easy line is Telegram: in Telegram open @BotFather, send `/newbot`, paste
the token it gives you as `TELEGRAM_BOT_TOKEN` (Settings → Telegram does it),
press **Pair** there (or `python scripts/telegram_setup.py pair`) and send
the six-digit code to the bot from your phone. That fills in
`TELEGRAM_OWNER_ID`; nobody has to know their Telegram id. No phone number,
no Facebook, no 24-hour window. Two things people hit: the bot must be
messaged from a **private** chat (a group never counts), and only one thing
may poll a bot at a time — the setup script pairs through the running
server for that reason, and a `409` in the status means something else is
polling. `docs/telegram.md` has the rest.

## Giving him a WhatsApp number (optional)

Three settings, all from [Kapso](https://kapso.ai) except your own number:
`KAPSO_API_KEY` (app.kapso.ai → Integrations → API keys),
`WHATSAPP_PHONE_NUMBER_ID` (`python scripts/whatsapp_setup.py numbers` prints
it once the key is set — it is Meta's numeric id, not the phone number) and
`WHATSAPP_OWNER_NUMBER` (E.164; `JARVIS_OWNER_PHONE` is the fallback). Settings
→ WhatsApp takes the same three and needs no restart.

Two things people hit:

- **He can only write to you within 24 hours of your last message to him.**
  Save his number and send it anything first. For the day after that, create
  the template: `python scripts/whatsapp_setup.py template`, wait for
  `template-status` to say APPROVED, set `WHATSAPP_TEMPLATE=jarvis_attention`.
- **There are no calls.** Meta's calling API needs a WebRTC media stack and
  is not offered from US numbers; a voice note in his voice is what he sends
  for the urgent lines. Do not promise a call. `docs/whatsapp.md` has the
  whole story, including why the line is polled rather than webhooked (the
  server must stay loopback-only).

## If something here is wrong

Every claim above cites the file that makes it true. If the cited code has
changed and the claim no longer holds, say so rather than repeating it —
a wrong setup claim costs a new user real time.
