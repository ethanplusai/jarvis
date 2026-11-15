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
`claude_env.child_env()` strips every `ANTHROPIC_*` variable (and
`CLAUDE_CODE_*`, `CLAUDECODE`) from the environment handed to every spawned
Claude Code child — brain and run pipeline alike — for one reason: the CLI
silently *prefers* an inherited `ANTHROPIC_API_KEY` over the logged-in
session, without saying so (`claude auth status` still reports
`loggedIn: true` while billing moves to the key). So:

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

## The voice is macOS `say`, with no key and no setup

`tts.py` no longer uses Fish Audio. It runs `say -o <tmp>.wav` for each
sentence chunk with `MAC_VOICE` (default Daniel). If that voice fails, it
falls back to the system default voice. `FISH_API_KEY` is still read by
`server.py` but ignored, so a missing key is harmless. If JARVIS is silent,
check `jarvis.log` for `say failed` or `TTS timed out`. The page also opens
muted until a double clap unmutes it (`clap_listener.py`), which can look
like a broken voice.

## Connecting a service: one file, and it is not the one you'd guess

JARVIS ships with two local MCP servers of his own: `mac_mcp.py` (Mac
control, Notes, Reminders, Calendar events, timers) and `briefing_mcp.py`
(Calendar, Reminders, weather, headlines). Beyond those he connects to
whatever the user brings, through MCP, and the whole of that is
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
- **The name.** Tools arrive as `mcp__<server>__<tool>`, so a name with a
  space, a slash or a `__` in it is refused. So is `jarvis`, which is his own.
- **He was not restarted.**

### Every tool costs context on every turn

Measured against `claude` 2.1.259 with JARVIS's exact flag set: about **250
tokens per tool**, resident in every single turn — a twelve-tool server is
~3,300, and JARVIS's own thirty-one are ~7,600. Five servers is real money on
a subscription and real latency on every reply. `connections` says the figure
out loud. It no longer costs the user any *memory* — `brain.py` measures the
resident floor on the warm-up turn and the rotation budget is spent on the
conversation, not the tool schemas — but "connect what you will use" is still
the advice.

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

## If something here is wrong

Every claim above cites the file that makes it true. If the cited code has
changed and the claim no longer holds, say so rather than repeating it —
a wrong setup claim costs a new user real time.
