# Operating and recovering JARVIS

## Messages and run history

The voice page includes a typed composer and durable conversation history.
Typing works without a microphone or speech-recognition support. A message
receives an ID before transmission. SQLite claims that ID once, so checking
or retrying its receipt cannot execute it twice. “Received” means the server
accepted the message, not that its requested work completed. Run status is
the source of truth for dispatched work.

Disconnected messages stay in the browser outbox. They are never silently
replayed on reconnect. Use **Send / check receipt** to explicitly deliver or
check them. Keep the browser profile to retain drafts; clearing site storage
removes them. Server conversation history survives browser reloads and server
restarts. A crash after receipt can interrupt handling; inspect run history
before submitting a new instruction with a new ID.

The dashboard fetches active runs separately from terminal history, so an old
active run cannot disappear behind 100 recent completions. **Load older runs**
uses a timestamp plus ID cursor, including runs with identical timestamps.
Project filtering applies to history. Token totals include input, output,
cache reads and cache creation. Retry preserves the requested model and
effective timeout; runs that never started do not resume a nonexistent session.

## Diagnostics and recovery

Open **Diagnostics & Data** on the voice page. It reports database readiness,
brain readiness, voice configuration, platform capabilities and the results of
explicit preflight checks. **Restart brain** restarts the brain and speech
services; **Open login** opens an interactive Claude terminal. Neither reports
successful authentication until checks observe it.

- `GET /api/health`: process liveness.
- `GET /api/readiness`: current service readiness and latest diagnostic checks.
- `GET /api/capabilities`: supported desktop operations and limitations.
- `POST /api/diagnostics/check`: refresh preflight results.
- `POST /api/diagnostics/repair/restart-brain` or `/open-login`: bounded repairs.

**Reading the latency line.** Every spoken turn logs one line:

```
latency: first_delta=1.39s first_cut=1.98s first_tts=1.61s first_audio=3.59s turn=1.98s ctx=26792 cached=26000 out=25 tools=[]
```

`first_delta` is when the brain's first text arrived; `first_cut` when the
first speakable chunk was cut from that text. The opening of a reply is held
for 0.6 s to see whether a tool call follows (`_OneLinePerTurn`: a turn that
uses a tool says one thing, at the end), so `first_cut` is normally
`first_delta` plus 0.6 s; the chunk itself ends at a sentence end, at a
strong break past 40 characters, or at its last clause break past 40 once
the sentence passes 80 characters, so a long opening sentence starts playing
while its tail is still being synthesized. `first_tts` is how long Fish took
to synthesize that chunk; `first_audio` when its audio left for the browser. `cached` beside
`ctx` is the part of the window that came from the prompt cache: a slow first
delta on an all-cached window is the model's time, on a fresh one it is a
cache being rebuilt every turn. Measured on 2026-09-24: Fish returns its
first byte after about 0.5 s and the whole chunk after 0.5 s plus roughly a
third of the audio's length (1.0 s for a 20-character line, 1.4 s for 60,
2.9 s for 120), so the length of the first sentence is what `first_tts`
mostly measures. The connection to Fish is opened while you are still
speaking (`_warm_tts`) and kept for a minute, so no turn pays for the
handshake.

Two of the preflight checks are about memory. `memory_index` warns when a
file in `memory/` has no line in `MEMORY.md` — the index is the only thing
that tells the brain a note exists at boot, so such a note is invisible to
him. Repair it from the dashboard's Memory tab ("Add them to the index") or
with `python maintenance.py reindex` while JARVIS is stopped; nothing repairs
it on its own, because a note without a line may be one you let go of
deliberately. `persona` warns when the brain's `CLAUDE.md` has been edited,
which stops every persona upgrade from applying: move your additions into
`LOCAL.md` beside it (read into every conversation, never overwritten) and
delete `CLAUDE.md`, and JARVIS writes the current one on the next start.

`mcp_launchers` reads `connections.json` and warns about any server started
through a package runner: `uvx`, `npx`, `pnpx`, `bunx`, `uv run`,
`uv tool run` or `pipx run`, including behind a `--` wrapper such as
mcp-subset or a `cmd /c` shell. Runs marked `--offline`, and `uv run` with
`--frozen` or `--no-sync`, are left alone. Those runners resolve packages on
every launch. The brain records its roster as soon as the CLI starts, so a
server still installing is logged `brain: MCP roster incomplete:
missing=<name>` and its tools are absent from the first turns. Install the
server into its own venv or node_modules and point `command` at the
executable. The spoken summary never repeats a server's name, because names
come from the user's file.

`claude_session_env` warns when JARVIS itself was started from inside a
Claude Code session — an agent running `python server.py`, say. A session
hands everything it starts its own identity and settings (`CLAUDECODE`, its
session id, its effort, its MCP start-up settings, its API timeout, its
trace, its inbox token). No Claude Code child of JARVIS receives any of them:
`claude_env.child_env()` removes every `ANTHROPIC_*`, `CLAUDE_*`, `MCP_*` and
`OTEL_*` variable and a handful of others, keeping only `CLAUDE_CONFIG_DIR`
and `CLAUDE_CODE_GIT_BASH_PATH`. But the backend's own environment still
reaches the browser and helpers it starts, and steering would send the
session's inbox token — good only for that session — to every session it
steers. The message lists the variables by name, never by value; the fix is
to restart with the scripts below, which start JARVIS without them. The same
check reports, without warning, a variable of your own under those prefixes
(an `MCP_TIMEOUT` in your profile): it is kept from JARVIS's children too.

**After upgrading Claude Code**, run `python scripts/probe_child_env.py`. It
starts the real `claude -p` against a fake API on 127.0.0.1 — a fake key, a
throwaway config directory, so no login is read and no quota is spent — once
per inherited variable, and reports which ones change what the CLI does
(effort, User-Agent, request count, MCP start-up, telemetry exports) and
what its own Bash child saw. It exits 1 if the CLI acts on a variable the
scrub lets through. Against CLI 2.1.270 and 2.1.280 it found
`CLAUDE_CODE_EFFORT_LEVEL`, `CLAUDE_CODE_ENTRYPOINT`,
`CLAUDE_AGENT_SDK_VERSION`, `API_TIMEOUT_MS`,
`MCP_SERVER_CONNECTION_BATCH_SIZE` and `OTEL_*` (with telemetry on) acted
on, and `CLAUDE_EFFORT`, `CLAUDE_PID`, `AI_AGENT` and
`MCP_CONNECTION_NONBLOCKING` not.

Then run `python scripts/probe_cli_echo.py`, the same way (fake API, no
login, no quota). JARVIS tells a message from another of your sessions
from your own turn by the CLI echoing each of its messages back
(docs/chatgpt-fallback.md, "Telling a wake from a turn"), and this checks
each thing that rests on: the echo comes before the turn's first output
and the result names the message, a message sent mid-turn is folded in
and echoed there, a message from another session says so, and a tag is
never run twice. It exits 1 if any has changed; against CLI 2.1.270 all
hold.

Saying **start fresh** (also "clear your head", "forget this conversation")
discards the current brain generation, so nothing it had read is carried
into the next one — that is what unblocks a memory write refused for
untrusted content. It deletes nothing: the conversation history in SQLite and
the CLI's transcript of that generation stay on disk, and JARVIS says so.
Individual messages can be deleted from the Conversation panel (two clicks).
That is a soft delete — the row keeps its id with a `deleted` stamp, because
the same table is how a typed message is accepted exactly once — so it leaves
history for good and a retried draft with that id is still a duplicate.

**Voice off.** The speaker button beside the microphone button silences
JARVIS without stopping him listening (the two are independent). While it is
off the server synthesizes nothing and sends each sentence as a `text`
frame, which the Conversation panel shows at once; the choice is kept in the
browser and re-sent on every connection, so it survives a server restart.

**Starting and stopping.** `scripts/start-jarvis.ps1` starts the backend and
the frontend as detached processes with logs under `data/logs/` and leaves an
already-listening port alone; `scripts/stop-jarvis.ps1` stops what it started.
Run from inside a Claude Code session, the start script clears that
session's variables (`python claude_env.py --inherited` names them) for the
two processes it starts, prints which, and puts them back in the calling
shell afterwards. JARVIS's own business credentials are not among them.
`scripts/install-autostart.ps1` registers a per-user scheduled task that runs
the start script 30 s after sign-in (not at boot: JARVIS needs the signed-in
desktop), at normal priority and on battery; its output goes to
`data/logs/autostart.log`, and `-Remove` unregisters it. The task finishing
does not stop JARVIS: the two halves are detached and outlive it.

Mutating routes use the same origin/host protections as other local actions.
Unavailable desktop actions are disabled in the Projects view. Capability
support does not guarantee that the target application is installed or OS
permissions have been granted; failed operations report their failure.

## Backup, export and retention

The data controls create downloadable archives or JSONL run exports under
`JARVIS_DATA_DIR/backups`. Archives include SQLite snapshots and files inside
the data directory, including private memory and internal configuration. They
exclude prior backups, live runtime markers, SQLite WAL/SHM sidecars, and
`jarvis/tool-token` — the bearer token that admits a caller to the memory
writers has no business in the same archive as the memory; a restored install
mints a new one on its next start. Two things are outside the data directory
and are NOT archived: the repository's `.env`, which must be preserved
separately, and the brain's own Claude Code transcripts under
`~/.claude/projects/<encoded brain home>/`, which belong to the CLI.
Archives are not encrypted; store them as private data.

Run these commands using the same environment and `JARVIS_DATA_DIR` as the server:

```text
python maintenance.py backup /absolute/path/jarvis.zip
python maintenance.py verify /absolute/path/jarvis.zip
python maintenance.py export /absolute/path/runs.jsonl
python maintenance.py prune --days 90
python maintenance.py reindex
python maintenance.py prune --days 90 --apply
python maintenance.py prune --days 90 --apply --transcripts
```

**Retention.** `prune` is one policy with one cutoff, and a report until you
add `--apply`. It covers finished runs (never a resume parent), conversation
rows, journal entries — all but the newest five, whatever their age, so the
boot handover always has a note — and the usage log (`usage_log.jsonl`),
which is rewritten with only the recent lines. With `--transcripts` it also
removes the brain's own Claude Code transcripts older than the cutoff: they
are the CLI's files, one per brain rotation (about ten a day), kept outside
the data directory, and only the brain's own directory under each config
root is ever touched. `scripts/start-jarvis.ps1` rotates its own log files
on every start and keeps the newest five of each.

Retention defaults to a preview. It deletes only old terminal runs and their
events, preserving active runs and referenced resume ancestors. The UI makes
a verified backup before applying retention. The CLI `--apply` is explicit
and does not make a backup automatically; create one first if desired.

## Restore

1. Stop JARVIS and any process using its data directory.
2. Run `python maintenance.py verify /absolute/path/jarvis.zip`.
3. Run `python maintenance.py restore /absolute/path/jarvis.zip`.
4. Restart JARVIS and check readiness and run history.

Restore checks member paths, checksums, archive sizes and SQLite integrity
before swapping the data directory. It rejects traversal, links, duplicate
members and Windows reserved paths. The previous directory is retained under
the restored directory's `backups` folder; the command prints its location.
A failed swap rolls back. An OS lock outside the data directory excludes a
concurrent restore or server startup, and a live runtime PID also blocks restore.

## Readiness and capabilities

Readiness now separates typed-turn readiness, login observation, TTS observation,
database accessibility and platform adapter support. Startup preflight results
are reused; observations older than five minutes become unchecked. A configured
Fish key does not claim working TTS: a successful synthesis establishes that
observation, while a failure clears it. **Run checks** refreshes preflight checks
without making a paid test synthesis. Bind host, scheme, port and their source
are reported from the same bind detection used for server authorization.

Platform capabilities also filter MCP discovery and the brain's tool allowlist;
the execution endpoint independently refuses unsupported desktop tools.

Business operations have a separate approval and persistence boundary; see
[Business desk](business.md). Restoring backups invalidates pending business
approvals to prevent replay of external operations.

The phone lines report themselves at `GET /api/telegram/status` and
`GET /api/whatsapp/status` (configured, paired or what is missing, whether
the poll is running, last sent and received, the last error), and the
`telegram` and `whatsapp` preflight checks warn about a half-finished setup
or a token waiting to be paired. Their tables (`telegram_messages` /
`whatsapp_messages`, the claim on every update or message id, and
`telegram_state` / `whatsapp_state`, the poll offset or cursor) live in
`jarvis.db` and travel with backups; a decision made from the phone is in the
card's audit trail as `via:telegram` or `via:whatsapp`. See
[JARVIS on Telegram](telegram.md) and [JARVIS on WhatsApp](whatsapp.md).

## Lifecycle guarantees and limits

Runs claim state transitions atomically in SQLite; only the winning terminal
transition publishes completion. Process ownership begins at launch: Windows
uses suspended launch followed by Job Object assignment, and POSIX uses a new
process session/group. Cancellation, timeout and shutdown terminate owned
descendants. Preflight checks and GitHub CLI calls use the same ownership
boundary; a timeout or cancelled lookup also reaps the helper and closes pipes.
Shutdown closes run admission, cancels active work, stops the
watcher and background tasks, then stops brain/speech before releasing runtime
ownership. Explicit application restart uses this same path.

Windows ownership follows Microsoft's [Job Object semantics](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).
Interactive desktop permissions and real Claude/Fish authentication require
local end-to-end verification; automated tests deliberately use fake services.

## What did JARVIS just do?

`GET /api/tool-calls` returns the PreToolUse gate's decisions, newest
first: the tool, the server, allow or deny, the reason the user would have
heard, and the digest tying an allowed call to the approval it spent.

```sh
curl -s -H 'Origin: http://localhost:5173'   'https://localhost:8340/api/tool-calls?limit=20' --insecure | python -m json.tool
```

Written before the call resolves, so a turn that dies mid-call still leaves
the row. Bounded to 20,000 rows. Not available as a brain tool: the
loopback token authenticates the brain, and this is the record of it.

The table is `tool_calls` in the usual database, so
`SELECT * FROM tool_calls ORDER BY seq DESC` works when the server is down.
