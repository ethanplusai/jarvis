# JARVIS — Voice AI Assistant

## Overview
JARVIS (Just A Rather Very Intelligent System) is a voice-first AI assistant for macOS. It runs locally on your machine, driving Claude Code for development tasks — every execution recorded as a *run* and watchable at `/dashboard`.

## Quick Start
Read `skills/jarvis-setup/SKILL.md` first — it carries setup and debugging
facts (mic-in-Chrome-only, the per-port permission trap, subscription vs. API
key, what an expired login sounds like, Accessibility) that were only learned
by hitting them live, and this walkthrough assumes them.

When a user clones this repo and starts Claude Code, help them:
1. Copy .env.example to .env
2. Install Claude Code (`npm install -g @anthropic-ai/claude-code`, 2.1.224 or
   newer) and log in with `claude` — JARVIS's brain runs on your Claude
   subscription, not on an API key
3. Get a Fish Audio API key from fish.audio (required — there is no fallback
   voice)
4. Install Python dependencies: pip install -r requirements.txt
5. Install the Playwright browser: python -m playwright install chromium
   (`read_page` / `look_at_page` need it)
6. Install frontend dependencies: cd frontend && npm install
7. Generate the SSL certs — see below, they are NOT optional:
   `openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj '/CN=localhost'`
8. Run the backend: python server.py --host 127.0.0.1
9. Run the frontend: cd frontend && npm run dev
10. Open Chrome (mic only works in Chrome) to http://localhost:5173
11. Click to enable audio, speak to JARVIS

**The certs are required for the dev-server workflow**, despite `server.py`
treating them as optional. `server.py` serves HTTPS only when `cert.pem` and
`key.pem` are both beside it, but `frontend/vite.config.ts` hard-codes its
proxy target as `https://localhost:8340`. Measured with a stand-in backend on
8340 and `npx vite`: with a plain-HTTP backend the Vite dev server itself
answers 200 while every proxied `/api` request returns **500**; with an HTTPS
backend the same request returns 200. The page loads either way, which is why
this presents as "the UI is up but nothing works" rather than as an error.

## Architecture
- **Backend**: FastAPI + Python (server.py, ~5800 lines)
- **Frontend**: Vite + TypeScript + Three.js (audio-reactive orb)
- **Communication**: WebSocket (JSON messages + binary audio)
- **AI**: a long-lived Claude Code process (`brain.py`, Sonnet by default) on
  the user's subscription; no Anthropic API calls on the voice path
- **TTS**: Fish Audio with JARVIS voice model
- **System**: AppleScript for Terminal and Chrome integration
- **Runs**: every Claude Code execution goes through one recorded pipeline —
  `run_store.py` (SQLite) + `run_executor.py`, surfaced at `/api/runs`,
  `/ws/runs`, and the `/dashboard` UI
- **Internal tool channel**: the brain reaches JARVIS's tools (session
  listing, steering, etc.) via a stdio MCP child that forwards
  `tools/call` to `POST /internal/tool` over loopback HTTP, bearing the
  token from `data_paths.ensure_tool_token()`.
- **Learning** (`learning.py`): a local LLM (any OpenAI-compatible server,
  `local_llm.py`) turns failed runs and broken tools into lessons, scores each
  lesson by the outcomes of the runs it rode along with against runs in the
  same projects without it (`learning_store.baseline`), merges lessons into
  strategies, and writes and repairs Python skills under sandboxed tests
  (`sandbox.py`). State is `learning_store.py` (SQLite, in jarvis.db).
  The local model only ever proposes: nothing it writes is used until the
  user approves it, except run lessons on trial under
  `JARVIS_LEARNING_AUTO_APPLY` that pass `looks_risky`
- **The web boundary** (`web_auth.py`): one ASGI gate above the router.
  Every WebSocket handshake and every state-changing request must carry
  either an `Origin` JARVIS serves from (localhost/127.0.0.1/[::1] on the
  API port or Vite's) or that same tool token. The browser needs no setup —
  it is same-origin through Vite's proxy, so it sends the `Origin` itself.
  There is no CORS: same-origin needs none, and sending none is what stops
  a hostile page reading the GETs that cannot be gated (a same-origin GET
  carries no `Origin` to check). The `Host` header IS checked on every
  request including reads, because DNS rebinding is the way around "no
  CORS". `--host` defaults to `127.0.0.1`; `0.0.0.0` still works and warns,
  and then needs `JARVIS_ALLOWED_ORIGINS`.

## The Run Pipeline
Nothing spawns Claude Code outside `RunExecutor`. Two invariants govern it:

1. **A run always reaches a terminal state.** `succeeded`, `failed`,
   `timed_out`, or `cancelled` — never left stuck in `running` or `queued`.
   Every exit path out of `_drive` writes a terminal status, and callers that
   create a run then hand it off must fail it if they throw before the
   executor takes ownership.
2. **Every state transition is a DB write BEFORE it is a notification.** The
   WebSocket is a cache-invalidation hint, never a source of truth; clients
   reconcile against `/api/runs`.

Two more rules for this area: no new npm or Python dependencies, and the
dashboard never uses `innerHTML` / `insertAdjacentHTML` — it renders
arbitrary LLM and file content, so everything goes through
`createElement` / `textContent`.

## Key Files
- `server.py` — Main server, WebSocket handler, HTTP API, action system
- `brain.py` — The voice brain: one long-lived `claude -p` process on the
  user's Claude subscription, fed over stdin as stream-json
- `jarvis_mcp.py` — Stdio MCP server exposing JARVIS's tools to the brain;
  forwards `tools/call` to `POST /internal/tool`
- `speech.py` — Sentence splitting and the scheduler that owns every
  utterance JARVIS speaks
- `tts.py` — Fish Audio synthesis, one request per sentence chunk
- `frontend/src/orb.ts` — Three.js particle orb visualization
- `frontend/src/voice.ts` — Web Speech API + audio playback
- `frontend/src/main.ts` — Frontend state machine
- `frontend/src/dashboard/` — The `/dashboard` run monitor (vanilla TS)
- `run_store.py` — SQLite `runs` / `run_events` tables, six-value status enum
- `run_executor.py` — Spawns `claude -p --output-format stream-json` and drives
  each run to a terminal state, streaming its events into the store
- `stream_parser.py` — Pure parsing of the stream-json output (no I/O)
- `builds.py` — The spec/brief/plan pipeline behind real, phased,
  multi-session builds (see "The Run Pipeline" above)
- `specs.py` — The review surface: reads back what JARVIS proposed and what a
  build produced
- `projects_view.py` — The Projects tab: a read-only JOIN over runs,
  sessions, plans and the repo, by project
- `session_watch.py` — Watches every Claude Code session on the machine
  (process / conversation / project)
- `session_steer.py` — Sends a message into a running session's inbox socket
- `dialog.py` — Answers a permission prompt in a Terminal window by sending
  it a keystroke (needs Accessibility)
- `notifier.py` — macOS notification fallback when no browser tab is
  connected to speak through
- `jarvis_memory.py` — Long-term memory: a folder of plain Markdown files the
  user can read and edit directly, not a database
- `usage_store.py` — Tracks the subscription's five-hour / seven-day
  rate-limit usage (there is no spend to report — see brain.py)
- `preflight.py` — First-run environment checks: `claude` CLI/login,
  Accessibility, Fish key, cross-session steering
- `claude_env.py` — The environment every spawned Claude Code child gets,
  including the `ANTHROPIC_*` scrub
- `actions.py` — System actions (Terminal, Chrome) via AppleScript
- `browser.py` — Playwright. Only the headless half is live (`read_page`,
  `capture_page`, behind `read_page` / `look_at_page`); the headful
  `JarvisBrowser` search/research class is reachable from nothing but
  `tests/test_browser_integration.py`
- `screen.py` — Seeing the Mac itself: the window list (`osascript`) and one
  downscaled `screencapture` the brain sees as an MCP image block. Captured
  only on a turn the user drove, never persisted, never on a timer
- `repo_read.py` — Cheap, model-free reading of a repository (no `claude`
  subprocess)
- `project_maker.py` — Creates a new project directory from a spoken name,
  path-validated against the projects root
- `work_mode.py` — Vestigial. `is_casual_question` is imported by `server.py`
  and called by nothing; the Haiku-vs-`claude -p` routing it classified for is
  gone. Left in place only because a test pins it
- `local_llm.py` — The learning system's model: OpenAI-compatible chat over
  httpx (Ollama, LM Studio, llama.cpp, vLLM). Off unless
  `JARVIS_LOCAL_LLM_MODEL` is set
- `sandbox.py` — Runs evaluated code and learned skills in a
  `python -I -S -X utf8` child: scrubbed env, timeout, rlimits on POSIX and a
  one-process Job Object on Windows. Makes mistakes cheap; it is NOT a
  wall against hostile code — the gates upstream are
- `learning_store.py` — SQLite `learning_*` tables: experiences, lessons
  (candidate / active / retired, with uses, outcomes and the baseline they
  are judged against), versioned skills,
  and the failing inputs each skill must survive
- `learning.py` — The engine: observe, reflect, apply, evaluate, consolidate,
  write and improve skills. Writes `LESSONS.md` (active brain lessons only)
- `data_paths.py` — Single source of truth for where data is written

## Where the author's own notes live
This repository ships nothing personal. Research notes, milestone
verification checklists and this project's own superpowers specs and plans
were moved to `.agents/`, which is ignored in full — do not re-add them, and
do not treat `git log` as their backup.

Note that `docs/superpowers/specs` and `docs/superpowers/plans` are still a
live convention: `builds.py` and `specs.py` create them **inside the projects
JARVIS builds**. It is only this repository's own copies that are gone.

## Environment Variables
- `ANTHROPIC_API_KEY` — **read by nothing.** Not asked for in setup, not read
  by any module, and the `anthropic` SDK is not a dependency: JARVIS's brain
  and every spawned run go through your Claude Code subscription login.
  `claude_env.child_env()` scrubs every `ANTHROPIC_*` variable from every
  spawned Claude Code child (brain and run pipeline alike) so the CLI can
  never bill an API key instead, and `preflight.py`'s `anthropic_key_leftover`
  check warns you if one is sitting in your `.env` — that is the only place
  the name appears in live code.
- `JARVIS_BRAIN_MODEL` (optional, default `sonnet`) — model for the brain,
  always passed explicitly as `--model`
- `JARVIS_BRAIN_AUTOSTART` (optional, default `1`) — `0` builds the brain but
  never spawns it (every test sets this)
- `JARVIS_MUTE_MIC_DURING_SPEECH` (optional, default false) — fallback if echo
  rejection is not enough with a given microphone
- `FISH_API_KEY` (required) — Fish Audio TTS
- `FISH_VOICE_ID` (optional) — Voice model ID
- `USER_NAME` (optional) — Your name for JARVIS to use
- `JARVIS_DATA_DIR` (optional) — Where JARVIS writes everything: the SQLite
  database, memory Markdown, usage.json, the tool token. Defaults to `data/`
  (`data_paths.py`). Set it to run an isolated instance without touching live
  data — the test suite gives every test a fresh one this way.
- `JARVIS_SKIP_PERMISSIONS` (optional) — Defaults to true; passes
  `--dangerously-skip-permissions` to spawned runs, which have no TTY to
  answer a permission prompt
- `WEATHER_LOCATION_LABEL` / `WEATHER_LATITUDE` / `WEATHER_LONGITUDE` /
  `WEATHER_UNIT` (all optional) — override the auto-detected (public-IP)
  weather location and units
- `JARVIS_LOCAL_LLM_MODEL` (optional) — turns learning on; the model name on
  the local server. `JARVIS_LOCAL_LLM_URL` (default
  `http://127.0.0.1:11434/v1`), `JARVIS_LOCAL_LLM_API_KEY`,
  `JARVIS_LOCAL_LLM_TIMEOUT` (default 120s) go with it
- `JARVIS_LEARNING_AUTO_APPLY` (optional, default off) — put reflected run
  lessons on trial without approval, and adopt improved versions of approved
  skills that pass their full suite and gain no new module. Brain lessons and
  new skills always wait for the user
- `JARVIS_ALLOWED_ORIGINS` (optional) — extra origins the web boundary
  accepts, comma-separated. Only needed when the page is opened at an
  address JARVIS does not serve from itself (a LAN IP, a tunnel). Their
  host names are also what the `Host` check accepts, so a `.local` or
  tailscale name has to be listed here to work at all
- `JARVIS_DEBUG_DOCS` (optional, default off) — serve `/docs`, `/redoc` and
  `/openapi.json`. That console has a "Try it out" button on every route
- `JARVIS_ENV_FILE` (optional) — where the settings endpoints read and write
  `.env`. Defaults to the repo's own; the test suite redirects it so no test
  can rewrite the developer's real configuration

## Testing
Run the suite as:

```bash
pytest
```

No flags. `pytest.ini` sets `testpaths` and deselects `-m "not browser"`, so a
bare run is the whole suite and touches neither the network nor the screen.

`tests/test_browser_integration.py` is marked `browser` (one `pytestmark` for
the file) because it drives `browser.py`, which launches Chromium with
`headless=False` on purpose and runs live searches. Run those with
`pytest -m browser`; they skip themselves if there is no network.

Everything else uses fakes at the subprocess seam — `screencapture`, `sips`,
`osascript` and `claude` are never really invoked. If you add a test that
needs the network, a real window, or a real `claude`, mark it.

## Conventions
- JARVIS personality: British butler, dry wit, economy of language
- Max 1-2 sentences per voice response
- The brain reaches JARVIS's capabilities as MCP tools (`jarvis_mcp.py` ->
  `/internal/tool`), not by parsing `[ACTION:X]` tags out of its reply — that
  tag machinery is gone in full, including the last handler (`_execute_browse`),
  which lost its caller with the voice dispatch chain
- AppleScript for Terminal and Chrome control (no OAuth needed)
- SQLite for runs, run events, usage and learning (`run_store.py`,
  `usage_store.py`, `learning_store.py`);
  long-term memory is plain Markdown files instead (`jarvis_memory.py`) —
  the user edits it directly, so it is never a database
