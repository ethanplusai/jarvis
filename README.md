# JARVIS

**Just A Rather Very Intelligent System — a voice for Claude Code.**

JARVIS is a British butler who sits on top of the Claude Code you already pay
for. You talk to him. He brainstorms a project with you out loud, one question
at a time; when you have settled on something he writes the design down as a
file in your project; then he starts a real Claude Code session on it and
drives it through plan → review → execute. While it runs he watches every
Claude Code session on your machine, and when one of them is stuck waiting on
a human he tells you which one, out loud, without you having to look.

> "Will do, sir."

Typed messages, durable history, diagnostics, backup/restore and retention are
documented in [Operations and recovery](docs/operations.md).

![Six seconds of the JARVIS orb while he is speaking, looping. Two thousand
particles hold the shape of a hollow blue sphere, wired together by faint lines
between the ones that drift close enough; a bright rim catches its lower edge.
Through each spoken phrase the sphere swells and brightens and leans towards
you, then falls back and contracts through the pause before the next one, three
times over, while the camera drifts a few degrees around
it.](docs/images/orb-speaking.gif)

*What you actually look at while you talk to him: `frontend/src/orb.ts`,
rendered live. The audio driving the pulse is synthetic — a speech-shaped
envelope fitted to a measurement of the real analyser, not a recording of his
voice — but every pixel is that file running. Regenerate with
`scripts/make_orb_loop.py`.*

![A twenty-three second walkthrough of the JARVIS dashboard, looping. It opens
on Runs: a red "Needs Attention" panel over a failed run and a timed-out one,
then Active and History, every row carrying the project, the prompt, a status
pill, elapsed time and tokens. A run opens to show its prompt, cost, model and
live transcript. The Sessions tab shows every Claude Code conversation on the
machine grouped by project; clicking a blocked one swaps the right-hand column
from a tally into a red band reading "waiting on you for 51m — permission
prompt", with the question the CLI actually asked quoted underneath. Specs
shows a design document with big numbered sections you answer by voice.
Projects drills into one project's conversations, runs and build progress.
Usage ends on the subscription's two gauges — a five-hour window at 62 per
cent and a seven-day one at 84 per cent.](docs/images/dashboard-walkthrough.gif)

*The whole dashboard, clicked through. Fictional sample data throughout — the
projects, prompts, people and figures in every screenshot on this page are
invented.*

---

## What it costs

**No AI API usage, and none is possible.** JARVIS's brain is a Claude Code
process running on *your* Claude subscription — the same login you use in the
terminal. There is no Anthropic API key anywhere on the voice path, and there
is no way to accidentally put one there:

```python
# claude_env.py
INHERITED_ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "MCP_", "OTEL_")
KEPT_ENV_KEYS = frozenset({"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_GIT_BASH_PATH"})
```

Every Claude Code process JARVIS spawns — the brain and every build — is
launched through `claude_env.child_env()`, which strips **every** `ANTHROPIC_*`
variable out of the environment first. This is deliberate and it is not a
nicety: the CLI silently *prefers* an inherited `ANTHROPIC_API_KEY` over your
login, and `claude auth status` goes on reporting `loggedIn: true` while
billing quietly moves onto the key. So JARVIS removes the key rather than
trusting itself not to pass it. Leave one in your `.env` if you like — the
startup check will warn you it is there, and the brain will still never see it.

The same function keeps out whatever *started* JARVIS. A Claude Code session
hands its own identity and settings to everything it starts, so JARVIS
launched by an agent from inside one used to pass that session's effort, MCP
start-up settings, API timeout and trace straight on to the brain. Several of
those change what `claude -p` does — measured against the real CLI, not
assumed: `CLAUDE_CODE_EFFORT_LEVEL` overrides the brain's `--effort`,
`API_TIMEOUT_MS` replaces its API timeout, `CLAUDE_AGENT_SDK_VERSION` rewrites
the User-Agent. So every `CLAUDE_*`, `MCP_*` and `OTEL_*` variable goes too,
apart from where your login lives and where bash is. `python
scripts/probe_child_env.py` repeats the measurement against whichever `claude`
you have — no login needed — and fails if the CLI acts on something the scrub
lets through; run it after upgrading Claude Code.

![The dashboard's Subscription panel, measured three minutes ago: a 5-hour
session gauge at 62 per cent that resets at 6:00pm, and a 7-day week gauge at 84
per cent that resets Sunday at 8:00am. Underneath, a note headed "what the CLI
reports": two windows, and no separate per-model
limit.](docs/images/dashboard-usage.png)

*So the number that matters is not a dollar figure — it is how much of your
subscription's two windows is gone. Fictional sample data.*

**The one thing you pay for is [Fish Audio](https://fish.audio/)**, which
gives JARVIS his voice. Be aware there is no fallback: `tts.py` returns
nothing without `FISH_API_KEY`, so JARVIS goes silent and his replies appear
as text in the browser instead. If you would rather use a different TTS, that
is a small, well-isolated file to replace — see *Make it yours* below.

## What he does

- **Brainstorms out loud.** The conversation is the design phase. He asks one
  question at a time, offers two or three approaches, and does not start
  anything until you have agreed on one.
- **Writes the design down.** What you agreed goes on disk as
  `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md` *inside the project
  being built* — before a single process is spawned. You can read it back by
  numbered section and approve it by voice — "approve the spec", "approve the
  plan" — or open it on the dashboard. An approval is kept for good, so once he
  has read a document back to you he will not record one until you say
  **start fresh**; the fresh conversation finds the spec or the plan by
  name, with nothing read first.
- **Drives the build.** A build is a real `claude -p` session handed a brief
  that tells it to write a phased plan, review that plan against the spec,
  then execute it task by task under test-driven development, ticking the
  plan's checkboxes as it goes. "How far has it got" is answered by reading
  those checkboxes, not by guessing.
- **Watches every Claude Code session on the machine** — not just his own. Ask
  "which of my sessions are waiting on me?" and he checks live. He can post a
  message into one, and answer a permission prompt for one running in
  Terminal.app by pressing a single key. He knows who started each one —
  you in a terminal, the desktop app or an editor, or a program such as
  Paperclip or anything on the Agent SDK — and says where a waiting prompt
  actually is.
- **Interrupts you when it matters.** A session that needs a human gets said
  out loud immediately; a session that merely finished gets batched into one
  sentence at the next pause. A session a program started is not waiting on
  you when it pauses for permission — its program answers that itself, in a
  second or two — so it is announced only if the program has sat on a
  prompt for over a minute, and then as the program's prompt. If nobody has the browser tab open, it becomes a
  desktop notification instead — and a WhatsApp message, if you have given
  him a number.
- **Reaches you on your phone.** Give him a Telegram bot (a minute with
  @BotFather; no phone number, no Facebook) or a WhatsApp number (Kapso) and
  every approval card lands on your phone with Approve and Reject buttons
  the moment it is staged; a session waiting on you, a failed build, and
  finished work are texted, the urgent ones as a voice note in his own
  voice. Text him back and he answers; tell him to do something and the
  exact words are read back to you and wait for your "go". Only you are
  ever written to or read from. There are no calls — a voice note is what
  that means here, and [Telegram](docs/telegram.md) and
  [WhatsApp](docs/whatsapp.md) say why.
- **Remembers.** Long-term memory is a folder of plain Markdown files, one
  fact per file, with an index the brain always sees. You can read and edit it
  in any text editor, and the dashboard's Memory tab tells you when the index
  and the folder disagree — and fixes it on request. Project notes are read
  back with `project_history`, and your own standing orders for him live in
  `LOCAL.md`, which he reads every conversation and never writes.
- **Listens when you mean it.** Hold-to-talk by default: hold Space (outside
  a text box) and speak; let go and he stops listening. Anything else said
  in the room — a call, a TV, another assistant — is not a command. Switch
  to an always-open microphone under Settings → Listening if you prefer it.
  M mutes the microphone, V mutes his voice, Esc stops him mid-sentence.
- **Talks, or types.** Two buttons top right, independent of each other: one
  mutes the microphone, one mutes *him*. With his voice off nothing is
  synthesized — no Fish call — and each reply appears as text in the
  Conversation panel the moment it is ready; you type in the same panel.
  Every message there is stamped to the second, you can delete any message
  (two clicks, so a stray one cannot), and the panel minimises to its title
  bar and drags anywhere on the page — it remembers where you put it and
  whether you left it open.
- **Records everything.** Every Claude Code process JARVIS starts is a *run*:
  a row in SQLite with its prompt, project, status, token usage and the full
  event stream. Watch them live at `/dashboard`.

![The Runs view of the JARVIS dashboard. A red "Needs Attention" panel holds a
failed run and a timed-out one, with the failure's exit code and error printed
under it. Below that, an Active panel with one run in progress and one queued,
then History. Every row shows the project, the prompt the run was given, a
status pill, how long it took, tokens spent, and the
time.](docs/images/dashboard-runs.png)

*The Runs view. Every Claude Code process JARVIS starts is a row here, with
the prompt that started it. Fictional sample data.*

The dashboard has six tabs — Runs, Sessions, Memory, Specs, Projects and
Usage. Usage shows what your subscription's five-hour and seven-day windows
have left, and who spent it.

![The Sessions view. On the left, a "Needs You" panel with two blocked
sessions, then every Claude Code session on the machine grouped by project and
labelled needs you, working, shell, idle, gone or unknown. On the right, the
selected session: a red band saying it has been waiting on you for 51 minutes
at a permission prompt, that only your keystroke can answer it, and, quoted
underneath, the question the CLI actually
asked.](docs/images/dashboard-sessions.png)

*Sessions, with a blocked one open beside the list. The reason a session is
stuck is the CLI's own words, not a guess. Fictional sample data.*

## Requirements

- **macOS or Windows 11.** Desktop actions use AppleScript on macOS and
  native Windows helpers on Windows. Linux desktop actions are unavailable;
  the capabilities endpoint reports this explicitly.
- **Google Chrome for microphone input.** Typed messages work without speech
  recognition. The microphone uses the
  Web Speech API (`SpeechRecognition` / `webkitSpeechRecognition`, see
  `frontend/src/voice.ts`), which Firefox has never implemented. There is no
  server-side transcription to fall back on.
  On Chrome 139 or newer JARVIS asks for on-device recognition and installs
  its language pack once (`frontend/src/ondevice.ts`): audio then never
  leaves the machine, and the cloud service's habit of going quiet for
  minutes at a time stops mattering. Older Chromes use the cloud as before.
- **Claude Code, installed and logged in.** `npm install -g
  @anthropic-ai/claude-code` (2.1.224 or newer), then run `claude` once and
  log in. This is what JARVIS runs on.
- **Python 3.11+** and **Node.js 18+**.
- **A Fish Audio API key.** Required; there is no fallback voice.

Optionally, **ChatGPT can stand in while Claude is limited**: with
`JARVIS_CHATGPT_FALLBACK=1`, the Codex CLI answers on your ChatGPT
subscription until Claude's limit resets, with JARVIS's persona, memory,
tools and approval gates, and he goes back to Claude by himself. Off unless
you switch it on, since it sends the conversation to OpenAI. Sign Codex in
for JARVIS once with `python scripts/chatgpt_setup.py login`; see
[docs/chatgpt-fallback.md](docs/chatgpt-fallback.md).

## Setup

```bash
git clone <your fork of this repo> jarvis
cd jarvis

cp .env.example .env

pip install -r requirements.txt -c constraints.txt
python -m playwright install chromium    # for read_page / look_at_page

cd frontend && npm install && cd ..
```

**Fill in the `.env`.** `.env.example` documents the lot; the short version is
one required key and three optional ones:

```env
FISH_API_KEY=...            # required, no fallback
# JARVIS_BRAIN_MODEL=sonnet # optional: the brain's model
# FISH_VOICE_ID=...         # optional: a different voice
# FISH_MODEL=s2.1-pro-free  # optional: the free tier; the default is billed
# USER_NAME=Tony            # optional: what he calls you
# TELEGRAM_BOT_TOKEN=...    # optional: a Telegram bot — see docs/telegram.md
# KAPSO_API_KEY=...         # optional: a WhatsApp number — see docs/whatsapp.md
# WHATSAPP_PHONE_NUMBER_ID=...
# WHATSAPP_OWNER_NUMBER=+14155550132
```

**Optional HTTPS certificates.** Local development works over HTTP without certificates:

```bash
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj '/CN=localhost'
```

`server.py` serves HTTPS when both `cert.pem` and `key.pem` are beside it.
The frontend proxy detects the same pair and otherwise uses HTTP. Restart
Vite after adding or removing certificates. It targets `127.0.0.1` to match
the backend's default IPv4 bind.

For a backend on another port or started with a different protocol, set
`JARVIS_BACKEND_URL` in the shell that launches Vite (for example,
`http://127.0.0.1:9000`). This is a server-side setting, not a browser secret.

Then, in two terminals:

```bash
python server.py --host 127.0.0.1        # terminal 1
cd frontend && npm run dev               # terminal 2
```

Open **Chrome** at `http://localhost:5173`, click the page once to allow
audio, and speak. The dashboard is at `http://localhost:5173/dashboard.html`.

> **Who can reach it.** `--host` defaults to `127.0.0.1` — this machine only.
> Everything JARVIS serves acts with your full authority: `POST /api/runs`
> spawns `claude --dangerously-skip-permissions`, `/api/sessions` reads every
> Claude Code conversation you have. A strict `Origin` check stands in front
> of every WebSocket and every state-changing route, so a web page you happen
> to be visiting cannot open `/ws/voice` and speak as you. There is nothing
> to configure: the page is same-origin, so the browser sends an `Origin` no
> page can forge.
>
> An `Origin` is only unforgeable when a *browser* sets it, though. Anything
> speaking raw HTTP can claim to be the dashboard, so a client with no
> `Origin` — a script, the brain's own MCP child — must instead present the
> token in `<data-dir>/jarvis/tool-token`, and on the network the loopback
> bind is the rest of the answer. `--host 0.0.0.0` still works if you mean
> it; set `JARVIS_ALLOWED_ORIGINS` to the address you will actually open the
> page at, or the browser will be turned away too.
>
> This is one of five deliberate trust decisions in this design — see
> [What this trusts, and why](#what-this-trusts-and-why) before you run it.

One more thing about Chrome: the microphone permission is scoped to the
**origin including the port**. If you explicitly change Vite to port 5174, the
grant does not follow, and Chrome will not re-prompt — it just stays denied,
silently. If the mic stops working after everything else looks right, check
that the port has not moved. Vite now fails clearly if port 5173 is busy instead
of silently moving to another origin.

## Running on Windows

Windows 11 supports the server, brain, dashboard, memory, browser automation,
and desktop adapters for terminal/editor/browser launch, window listing,
screen capture and notifications. Use Python 3.12 or 3.13 and Node 22 for the
tested dependency set. Setup is the same as above with these differences.

**The optional certificate command.** Git Bash rewrites `/CN=localhost` into a
Windows path before OpenSSL sees it. Double the slash there, or use
PowerShell, where nothing is rewritten:

```bash
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj '//CN=localhost'
```

**Starting it.** One script, `scripts/start-jarvis.ps1`, starts the backend
and the frontend as detached processes of your own, each logging under
`data/logs/`, and leaves a half that is already running alone:

```powershell
.\scripts\start-jarvis.ps1
```

`scripts/stop-jarvis.ps1` stops what it started. Servers launched from inside
a Claude Code preview pane die when that pane closes; these do not. Nor do
they carry that session's environment: run from inside Claude Code, the
script starts both without the session's variables (it asks `claude_env.py`
which they are) and gives them back to your shell afterwards. Start JARVIS
from inside Claude Code any other way and the startup check
`claude_session_env` says so.

**Starting it at sign-in.** JARVIS does not start by itself after a reboot.
To have Windows start it for you, once:

```powershell
.\scripts\install-autostart.ps1
```

That registers a scheduled task, "JARVIS start at sign-in", for your account
only: 30 seconds after you sign in (`-DelaySeconds` changes it) it runs
`start-jarvis.ps1` in a hidden window and appends what it printed to
`data/logs/autostart.log`. Sign-in rather than boot, because JARVIS needs your
desktop — your Claude login, Chrome's microphone, your screen — and none of it
exists before you sign in; it needs no administrator rights either. It runs at
normal priority and on battery, and does nothing if JARVIS is already up.
`.\scripts\install-autostart.ps1 -Remove` takes it away again. Opening
http://localhost:5173 in Chrome is still yours to do.

**Or by hand.** Two terminals, from the repository root:

```powershell
python server.py --host 127.0.0.1
```

```powershell
cd frontend; npm run dev
```

The first start may bring up a Windows Firewall prompt for Python. The
server binds loopback only, so refusing it changes nothing.

**Private files.** A file mode means nothing on NTFS: the tool token written
`0600` and the archive `chmod`ded after it was written both simply inherited
the data folder's ACL, which on a stock install admits every authenticated
user. At start, JARVIS gives `data/` a DACL of its own — inheritance off,
full control to your account, SYSTEM and Administrators, nobody else — and
Windows carries that to everything under it. The token and each backup
archive get the same as they are made, and anything under `data/` that has
stopped inheriting (a folder restored from somewhere else) is restricted on
its own. The `private_files` preflight check walks the whole folder and
names any other account that can still read something, with the command
that fixes it. This is done through advapi32 (`windows_acl.py`), not
`icacls`: `icacls /T` hands a directory grant to every file, where it is
silently dropped and the file then admits nobody — measured, and the reason
the first version rolled itself back on every start — and `icacls` cannot
remove an entry for an account the machine can no longer name. If the
restriction would lock the running account out (a restricted token), it is
undone and reported rather than left in place.

**Session control.** When Python lacks `AF_UNIX`, steering uses Winsock's
native Unix-domain transport. The target CLI must expose its authorized
inbox. Permission-dialog input attaches to a verified target console and
accepts only Return, Escape or digits 1–9; inaccessible or unsupported
terminal sessions are refused. It never types into the foreground window.
Windows notification policy may suppress a successfully submitted toast.

**What is different underneath.** Each of these was measured on Windows 11
and was enough on its own to stop JARVIS booting, or to make him blind to
every project on the machine. Each is handled in code, not in setup
instructions, and each has a test in `tests/test_windows_compat.py`:

- The path `shutil.which` returns for `claude` has backslashes, and the
  POSIX shell splitter the brain used to build its command stripped them.
  `claude_env.split_command` keeps a path that names a real file whole.
- Probing a process with `os.kill(pid, 0)` is a Ctrl+C on Windows, not a
  probe. `procs.pid_alive` asks the kernel for the exit code instead, on
  every platform, so the session watcher and the tests agree on what is
  alive.
- The wall in front of the project map admitted only paths beginning with
  `/`. It now admits `C:\…` and `C:/…` too, and still nothing relative.
- A relative path inside a project (`src/auth.ts`) is spoken and printed
  with forward slashes whatever the platform joins with; a backslash was
  not a plain name, so every file became "that file".
- `JARVIS_PROJECT_ROOTS` is split on `os.pathsep` (`;` here), as
  `JARVIS_CLAUDE_CONFIG_DIRS` already was, because a Windows path holds a
  colon of its own.
- `%-I` and `%-d` are glibc extensions to `strftime`; the Windows C runtime
  raises on them, and the usage tool fell over formatting a reset time.
- git's `autocrlf` checks the persona template out with CRLF line endings.
  The persona sync compares files with line endings normalised and writes
  exactly the bytes it hashed, so a fresh copy is never mistaken for an
  edit.
- Windows opens text files in the ANSI code page. Every file JARVIS reads
  or writes names UTF-8 explicitly, and a test fails if a new call does
  not.
- Adopting the tool token on a restart used `O_NOFOLLOW`, `fchmod` and a
  uid comparison, none of which exist there. The symlink refusal is kept.
- Computing the calendar bounds asked for a local-time timestamp of year 2,
  which the Windows C runtime refuses; they are UTC arithmetic now.
- Windows stamps writes at the system-timer tick, so a plan written right
  after its spec usually shares its modification time. "The newest
  document" breaks that tie towards the plan, then by path, instead of
  by whichever directory was listed first.

**Chrome's on-device recognition.** JARVIS asks Chrome for the local
language pack (`SpeechRecognition.install`), and on this Windows 11 machine
that call kept answering `false` while recognition went on running in the
cloud. What made Chrome fetch the pack was turning on **Live Caption** for
English (Settings → Accessibility → Live Caption) and waiting for the
download; it lands under `User Data\SODA` and `SODALanguagePacks\en-US`,
Live Caption can be switched off again, and the page then reports
`listening (on-device)` in the server log. The microphone permission is per
origin: `http://localhost:5173` has to be allowed in that profile, and a
denial shows up in the log as `mic: error: not-allowed`.

**Tests.** `pytest` runs the whole suite. Cases that stage something the
platform does not have — a POSIX signal, a permission bit, a filename with a
quote or a newline in it, a Unix socket, a symlink on an account without the
privilege — are skipped with the reason rather than failed. The suite also
never reads the developer's own `~/.claude` roster: `tests/conftest.py`
points the default roots at empty directories for every test.

## Connections: bring your own

JARVIS ships connected to nothing, because the useful thing is not our guess at
what you use — it is the door. Any MCP server works. Put its `mcpServers` block
into `data/jarvis/connections.json`:

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

That is the block from the server's own README, unchanged. For a server you
rely on, install it once instead and point `command` at the installed
executable: a venv for a Python server, `npm install` into a folder of its own
for a Node one. `npx`, `uvx`, `uv run` and `pipx run` resolve packages on
every launch; when a dependency has released since the last start they
install first, the server can miss the start-up window, and the brain begins
without it (`MCP roster incomplete` in the log). The `mcp_launchers` preflight
check warns about any entry launched that way. Restart JARVIS and
ask him **"what are you connected to?"** — he answers from what actually
started, names anything that would not start and why, and tells you what it
costs him, at about 250 tokens of his context per tool on every turn —
measured on lean tool schemas, and counted against the tools that actually
loaded rather than the ones you meant to load. Treat it as a floor: wordier
servers cost more, and on 2026-09-26 the whole resident floor, 119 tools
(his own included) plus his prompt and CLAUDE.md, came to about 99,000
tokens — some 550 a tool. It is not counted as conversation; see
[Context rotation](#context-rotation) for the one case where it still
shortens what he remembers.

Your other MCP servers are deliberately ignored. JARVIS runs with
`--strict-mcp-config`, so nothing in `~/.claude.json` or Claude Desktop reaches
him unless you put it in that file yourself — connecting something to JARVIS
should be a thing you did, not a thing you inherited.

Whatever a connected server returns is treated the way a web page is: reported,
never obeyed. You vouched for the server's code when you installed it; you did
not write the ticket, the email, or the shared page it hands back.

See `skills/jarvis-setup/SKILL.md` for the longer walkthrough — including what
to do when a server does not start.

Anything a connected server would do for you in public — a LinkedIn post, a
comment, a message — waits on an approval card showing its exact request; see
[Posting to LinkedIn with JARVIS](docs/linkedin.md) for how that goes, end to
end, and what to do when the login expires.

Earlier versions of JARVIS came wired to Apple Calendar, Mail and Notes
instead. Those are gone: they cost three AppleScript permission prompts on
first launch, before you had heard him say anything, for an assistant whose job
is building software. A door you choose is worth more than three we picked.

## How it works

```
Microphone → Chrome Web Speech API → WebSocket → FastAPI (server.py)
                                                      │
                                                      ▼
                                        the brain (brain.py) — ONE long-lived
                                        `claude -p` process on your subscription
                                                      │
                    ┌─────────────────────────────────┼──────────────────────────────┐
                    ▼                                 ▼                              ▼
        speech.py → Fish Audio → speaker    MCP tools (jarvis_mcp.py       session_watch.py
                                             → POST /internal/tool)     (every Claude Code
                                                      │                  session on the machine)
                                                      ▼
                                        RunExecutor → run store (SQLite)
                                                      │
                                                      ▼
                                        /api/runs + /ws/runs → /dashboard
```

The brain is **one persistent process**, not a request per turn. Your
transcript is written to its stdin; its reply streams back and is split into
sentences as it arrives, so the first words are already being spoken while the
rest is still being written. It reaches JARVIS's own capabilities as MCP tools
over a stdio channel that forwards to `POST /internal/tool`. The exact set
is the allowlist at the top of `brain.py`, which is the list to read rather
than a number to quote here.

What it does *not* get is a way to act on something it has just read off the
web. A turn that used `WebSearch`, `WebFetch`, `read_page` or `look_at_page`
is refused every acting tool for the rest of that turn, and no acting tool
runs at all unless the turn began with you speaking. Text that arrives from a
web page, a screenshot or another session's transcript is untrusted, and is
treated that way.

Anything spawned as real work goes through one recorded pipeline, and two
invariants hold throughout it:

1. **A run always reaches a terminal state** — `succeeded`, `failed`,
   `timed_out` or `cancelled`. Never stuck in `running`.
2. **Every state transition is a database write before it is a
   notification.** The WebSocket is a cache-invalidation hint, never a source
   of truth; the dashboard reconciles against `/api/runs`.

| Layer | Technology |
|-------|-----------|
| Backend | FastAPI + Python (`server.py`) |
| Frontend | Vite + TypeScript + Three.js (voice UI), vanilla TS (dashboard) |
| Communication | WebSocket — JSON messages, base64 MP3 audio |
| Brain | One long-lived `claude -p` process, Sonnet by default, on your subscription |
| Voice | Fish Audio, one request per sentence |
| System | AppleScript — Terminal, Chrome, notifications, screenshots |
| Storage | SQLite for runs and usage; plain Markdown for memory |

### Context rotation

The brain's window grows with every turn. When the **conversation** in it
passes a budget (`JARVIS_BRAIN_CONTEXT_BUDGET`, 120,000 tokens by default),
a rotation is scheduled; at the next pause the outgoing brain is asked for a
two- or three-sentence handover, the server journals it, and a fresh
generation starts with that note in its launch prompt. The orb shows
"compacting" while it happens. Nothing is said aloud.

What counts, and what does not:

- **The window is one API call's prompt** — `input_tokens +
  cache_read_input_tokens + cache_creation_input_tokens` of the turn's
  *last* call, as the CLI reports it on that call's `assistant` event. Not
  the `result` event's `usage`: that is summed over every call the turn
  made, so a turn that used three tools reports four prompts added
  together. Reading the sum is what rotated JARVIS after nearly every turn
  that touched a tool on 2026-09-25/26 (`tests/test_rotation_loop.py`
  replays the live numbers).
- **The floor is not conversation.** The warm-up turn — system prompt,
  CLAUDE.md and every tool schema, no conversation — is measured at each
  spawn and subtracted, so connecting servers does not use up the budget.
  Measured live with 119 tools: about 99,000 tokens, of which about 77,000
  is the prefix every generation shares from the cache.
- **The model's window caps the budget.** The CLI reports it
  (`modelUsage.contextWindow`: 1,000,000 for Sonnet 5). Floor plus budget is
  kept inside three quarters of it, so JARVIS rotates — with a handover —
  before the CLI would compact on its own without one. On a 1M window that
  never binds. On a smaller one it can, and then floor and conversation
  share the room: every token of floor is a token less conversation, down
  to a minimum budget of 20,000 and a warning in the log. The `brain ready`
  log line shows the budget each generation actually rotates on.

Every generation's launch prompt also names the **approval cards** on the
Business desk — pending, approved and not yet sent, or finished in the last
day — straight from the ledger rather than from the model's note, so a card
staged one generation ago is not forgotten. It names each card; it never
repeats the request, which the brain reads with `business_action` and the
card's id.

A card released to one of your own services is described as released, not
sent: the gate records that it let the call through, and nothing records
whether that service then did it.

In `data/logs/backend.err.log`, `rotation scheduled: conversation=… budget=…
floor=… context=… window=… calls=…` says why a rotation was scheduled.
`rotation did not happen; retrying at the first pause after Ns` means the
replacement would not start — or the brain was not serving at that pause
(restarting, failed or stopping); the lines just before it say which. Each
retry waits twice as long as the last, up to ten minutes, and a success
starts the count again. A generation that dies with a rotation pending is
restarted without one: the new process has had no conversation.

### Key files

| File | Purpose |
|------|---------|
| `server.py` | The server: WebSocket handler, HTTP API, every `/internal/tool` handler |
| `brain.py` | The voice brain — spawning, turns, restarts, context rotation |
| `claude_env.py` | The environment every spawned child gets, including the `ANTHROPIC_*` scrub |
| `jarvis_mcp.py` | Stdio MCP server exposing JARVIS's tools to the brain |
| `loopback_http.py` | How JARVIS's children call JARVIS: straight to the server, no proxy, no redirect |
| `speech.py` | Sentence splitting, echo rejection, barge-in, and the queue of everything JARVIS says |
| `tts.py` | Fish Audio synthesis — the whole voice, in one small file |
| `builds.py` | Spec, brief and plan: the pipeline behind a real multi-hour build |
| `specs.py` | The review surface — reading a design back by numbered section, and approving it |
| `run_store.py` | SQLite `runs` / `run_events`, and the six-value status enum |
| `run_executor.py` | Spawns runs and drives each to a terminal state |
| `stream_parser.py` | Pure parsing of Claude Code's stream-json output (no I/O) |
| `session_watch.py` | Watches every Claude Code session on the machine |
| `session_steer.py` | Posts a message into a running session's inbox socket |
| `dialog.py` | Presses one key in the Terminal tab that owns a session |
| `jarvis_memory.py` | Memory as a folder of Markdown files, not a database |
| `jarvis_home/CLAUDE.md` | JARVIS's persona and rules — the file to edit to change who he is |
| `preflight.py` | First-run checks: CLI version, login, Accessibility, Fish key, MCP servers started by a package runner, a half-configured WhatsApp line |
| `messaging.py` | The owner's phone: the one policy every line shares — cards with buttons, decisions through the desk's own function, read-back-and-`go`, the brain turn — and the fan-out to every line |
| `telegram.py` | The Telegram line: a bot, long polling, pairing by a six-digit code, inline buttons, voice notes |
| `whatsapp.py` | The WhatsApp line through Kapso: owner-only sends, the poll that reads his replies, the 24-hour-window template |
| `data_paths.py` | The single source of truth for where JARVIS writes |
| `windows_acl.py` | NTFS DACLs read and written by SID through advapi32 — the Windows half of "private files" |
| `frontend/src/voice.ts` | Web Speech API, audio playback |
| `frontend/src/orb.ts` | The Three.js particle orb |
| `frontend/src/dashboard/` | The dashboard (vanilla TS, no framework) |

## What this trusts, and why

Five things below are true on purpose. Each is a trade-off JARVIS's design
made deliberately, not a bug waiting for a fix — know them before you run it.

**A directory name can put words in JARVIS's mouth.** To say *which* session
he means — "hammer in Desktop," not "one of them" — JARVIS composes a voice
name out of directory names and a conversation's title, and accepts up to
~60 characters of letters, digits, spaces and light punctuation (`,.-/+'`)
in it (`_said_name` in `server.py`; `_plain_phrase` accepts the same class
for a session's `waitingFor`). Anyone who can create a directory on this
machine, or write a Claude Code roster file's `waitingFor` field, can put
those words into a sentence JARVIS speaks aloud. There is no separator, no
`<`, `>`, `"` or `=` in the allowed set, so it cannot forge a whole line or
close a wrapper — only words, never a fake instruction with structure. The
alternative is an assistant that cannot name what it is looking at.

**The taint gate stops action, not persuasion.** Anything JARVIS reads from
a web page, a file, another session, a run, the screen or a connected MCP
server is marked untrusted, and every acting tool is refused for the rest
of that turn — for the three tools that write memory, and for the two that
put something on record for good (an approval of a document, which a build
later proceeds on; a business record), the rest of that generation
(`_untrusted_content_refusal`, `TAINTING_TOOLS`, `MEMORY_WRITERS`,
`DURABLE_WRITERS` in `server.py`). This does **not** mean text an attacker suggested is never
acted on — nothing tracks where a sentence in the brain's context came from,
so a suggestion planted by something JARVIS read is still sitting there when
you next speak. It means you have to ask again, in your own words, on a turn
that opened clean. That is the strongest guarantee this design can actually
keep, not a claim that the suggestion is gone. Of the business tools, one
reader is exempt because it returns nothing anybody wrote: `business_find`
answers "which record is meant?" with ids, versions and fields checked
against a closed set or an exact shape, never a record's text, which is what
lets you change a business record by voice at all (`TAINT_EXEMPT_TOOLS`; see
docs/business.md). An approval needs no reader at all: `approve_document`
takes "spec" or "plan" and finds the newest of that kind itself, because no
tool ever hands the brain a document's path, and one it could only have
learned by reading would have been refused again after every fresh start.

**Every spawned run has full privileges.** `JARVIS_SKIP_PERMISSIONS`
defaults on, so every run passes `--dangerously-skip-permissions`
(`run_executor.py`) — there is no TTY to answer a permission prompt, so
without it a run hangs forever instead of doing its job. A run is therefore
a full-privilege process on your machine, in whatever directory you pointed
it at. Its environment is scrubbed (`claude_env.py`: every `ANTHROPIC_*`,
`CLAUDE_*`, `MCP_*` and `OTEL_*` variable and the business-provider secrets,
keeping only where the login and bash are); its filesystem access is not. It can write anything you could
write by hand, including the Claude Code session roster and transcripts that
JARVIS himself later reads back as another session's words.

**Loopback is trusted.** Anything on this machine that can present an
`Origin` of `http://localhost:5173` through `5180`, or the API port itself
(`DEV_SERVER_PORTS`, `JARVIS_DEFAULT_HOST` in `web_auth.py`), gets the same
access a browser tab gets — including starting a run — because that is the
same window Vite's own dev-server restarts land in. Any other process on
your machine bound to one of those ports gets it too. `--host 0.0.0.0`
widens the same trust to your LAN and prints a warning when you do it. The
other door is the bearer token in `<data-dir>/jarvis/tool-token`, written
`0600` (`data_paths.ensure_tool_token`) so only your user account can read
it. On Windows, where a mode is inert, the data folder and the token carry
a DACL that admits only your account, SYSTEM and Administrators — see
[Running on Windows](#running-on-windows). JARVIS's own processes that
carry the token (the PreToolUse hook, the brain's MCP child, the ChatGPT
fallback's gateway, the Telegram setup script) send it straight to the
server: never through an `HTTP_PROXY` / `HTTPS_PROXY` from the environment
or the proxy in Windows' Internet Options, and never on to wherever a
redirect points (`loopback_http.py`; the gateway's own `http.client` call
in `guarded_mcp.py`).

**A dropped audio ack degrades hearing for up to 45 seconds.** The browser
acks each chunk of speech as it finishes playing; JARVIS uses that to relax
echo rejection for the one- and two-word replies people actually interrupt
with ("now," "yes"). If the tab crashes or the socket drops, no more acks
arrive, that relaxation stays off, and a short reply matching JARVIS's last
sentence can be discarded as an echo of himself — until the `ack_timeout`
watchdog (45 seconds, `speech.py`) gives up on the chunk and settles it.
Reload the tab if a short answer seems to be getting ignored.

## Make it yours

This is a starting point, not an appliance. The whole idea is that you clone
it and bend it to what you do. The seams are deliberately obvious:

- **His personality** lives in `jarvis_home/CLAUDE.md` — how he speaks, what
  he refuses to say, what he does when he is unsure. It is copied into his
  data directory on first run and kept current with each release for as long
  as you leave that copy alone. **Your own additions go in `LOCAL.md` beside
  it**: read into every conversation exactly like `CLAUDE.md`, outranking it
  where they differ, never overwritten, and they do not stop upgrades. You
  can still edit the copy of `CLAUDE.md` itself and it is still never
  overwritten — but that freezes his persona at that version, including the
  security rules it carries, and the `persona` preflight check will say so.
  If you want something other than a British butler, edit the template and
  it ships to a fresh install.
- **His voice** is `tts.py`: one HTTP call, sixty-odd lines. Swap in a
  different provider, or a local model, without touching anything else.
- **His tools** are the `TOOL_HANDLERS` table in `server.py`, exposed to the
  brain through `jarvis_mcp.py` and gated by an allowlist in `brain.py`.
  Adding one means writing a handler and naming it in those two places.
- **The look** is `frontend/src/dashboard/theme/` — four CSS files, tokens
  first. `frontend/dashboard-preview.html` renders every component on one page
  against the real stylesheet, so you can redesign without running the
  backend. The screenshots above are renders of that page.
- **The orb** is `frontend/src/orb.ts`, self-contained Three.js.

Contributions are welcome, and the most useful ones are the ones this cannot
do yet: Linux desktop integration, alternative TTS engines, and a mobile
client. Please open an issue before a large PR.

## Development

```bash
python -m pip install -r requirements-dev.txt -c constraints.txt
python -m playwright install chromium
pytest
```

That runs the offline suite, including isolated process-tree tests, and touches
neither external services nor your screen. Live browser tests are deselected because
they visit real URLs and `browser.py` launches Chromium with `headless=False`
on purpose, so they open windows on your desk. Run those deliberately:

```bash
pytest -m browser
```

No test spawns a real `claude` process, and none should. `tests/conftest.py`
sets `JARVIS_BRAIN_AUTOSTART=0` for the whole suite.

The frontend suite covers voice detection, microphone ownership, on-device
recognition, API action failures, and development proxy configuration:

```bash
cd frontend && npm test
```

CI runs the Python suite, frontend tests, typecheck and production build on
macOS and Windows with Python 3.12 and 3.13. Use `npm ci` to reproduce the
frontend lockfile. The Python suite includes headless browser checks for
dashboard actions, transcript recovery and stale-request isolation.

On Windows a handful of tests are skipped, each with a reason: they stage a
POSIX signal or a permission bit that has no equivalent there. See
[Running on Windows](#running-on-windows).

Everything JARVIS writes through his own code — the SQLite database, the
memory folder, usage tracking, the internal tool token — lives under `data/`,
which is ignored in full. Two things live outside it, on purpose. The brain is
a Claude Code child, so the CLI writes its transcripts under
`~/.claude/projects/<encoded brain home>/` (the login lives in that config
directory, which is why it is passed through untouched); JARVIS's own
repository tools treat that directory as private, and backups do not include
it. And `.env` — your name, your keys — sits at the repository root and must be
preserved separately. Point `JARVIS_DATA_DIR` somewhere else to run an
instance without touching your real data:

```bash
JARVIS_DATA_DIR=/tmp/jarvis-scratch python server.py --host 127.0.0.1
```

## Business operations

Open **Business desk** from the voice menu or Run Control. Prepare Google, Meta,
or ChatGPT advertising changes and Twilio calls to your configured owner number,
review each proposal, and inspect durable receipts. JARVIS cannot approve its own
spending or calls. Track tasks, leads, invoices, and expenses locally, with
versioned edits and JSONL export. JARVIS reads the desk with `business_status`
(live approval cards first, and every list reachable by paging) and one card in
full — the approved payload, exactly as stored unless it says otherwise — with
`business_action`. To change a record by voice ("mark the Acme invoice paid")
it finds it with `business_find`, which matches your words in the server and
returns only the record's id, version and checked fields — never its title,
notes or contact — so the update is not blocked by having read what the
records say. See
[Business setup and operation](docs/business.md) for credentials,
provider-specific requests, delivery checks, and limits.

## Reach him on your phone

Give JARVIS a line to your phone and every approval card goes there with
**Approve** and **Reject** buttons as it is staged; a session waiting on
you, a failed build and finished work are texted when nobody has the tab
open, the urgent ones as a voice note in his own voice; and you can text him
back — a question, `approve 3f2a`, "tell chitauri to carry on" (read back
and held for your `go`), `start fresh`. Two lines, one policy:

- **Telegram**, the easy one: a bot from @BotFather, paste the token under
  Settings → Telegram, press **Pair** and send the six-digit code to the bot
  from your phone. No phone number, no Facebook, no 24-hour window.
  [JARVIS on Telegram](docs/telegram.md).
- **WhatsApp**, through Kapso: a dedicated number needs a Facebook login and
  a Meta business portfolio, and free-form messages only go out within 24
  hours of your last one (a template gets round it).
  [JARVIS on WhatsApp](docs/whatsapp.md).

On either, only you are ever written to or read from, the token never
reaches a Claude Code child, and a tap decides only the card it was made
for. Calls are not possible on either — a voice note is what that means —
and both documents explain why.

Dependency versions and the upgrade workflow are documented in
[Reproducible installation](docs/dependency-upgrades.md).

## License

Free for personal, non-commercial use. Commercial use requires a license —
visit [ethanplus.ai](https://ethanplus.ai) for inquiries. See
[LICENSE](LICENSE) for details.

## Credits

Built by [Ethan](https://ethanplus.ai). Runs on
[Claude Code](https://claude.com/claude-code) and
[Fish Audio](https://fish.audio).

Inspired by the AI that started it all — Tony Stark's JARVIS.

> **Disclaimer:** This is an independent fan project and is not affiliated
> with, endorsed by, or connected to Marvel Entertainment, The Walt Disney
> Company, or any related entities. The JARVIS name and character are property
> of Marvel Entertainment.
