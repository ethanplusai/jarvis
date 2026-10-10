# JARVIS — Voice AI Assistant

## Overview
JARVIS (Just A Rather Very Intelligent System) is a local voice and typed assistant for macOS and Windows, driving Claude Code for development tasks — every execution recorded as a *run* and watchable at `/dashboard`. Platform capabilities and limitations are reported by `/api/capabilities`.

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
4. Install Python dependencies: pip install -r requirements.txt -c constraints.txt
5. Install the Playwright browser: python -m playwright install chromium
   (`read_page` / `look_at_page` need it)
6. Install frontend dependencies: cd frontend && npm ci
7. Optionally generate SSL certs for HTTPS:
   `openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj '/CN=localhost'`
8. Run the backend: python server.py --host 127.0.0.1
9. Run the frontend: cd frontend && npm run dev
10. Open Chrome (mic only works in Chrome) to http://localhost:5173
11. Click to enable audio, speak to JARVIS

**Certificates are optional for local development.** The Vite proxy and
`server.py` both select HTTPS only when `cert.pem` and `key.pem` exist;
otherwise they use HTTP. Restart Vite when changing the certificate pair.
The proxy uses `127.0.0.1:8340` by default; set `JARVIS_BACKEND_URL` in its
launching shell for a custom backend origin. `strictPort` keeps the frontend
on 5173 and reports a conflict instead of changing microphone permissions.

## Architecture
- **Backend**: FastAPI + Python. `server.py` (~7500 lines) holds the voice
  socket, the brain, the tool handlers and the gates; the HTTP surfaces for
  usage, settings, memory and conversation are routers in modules of their
  own (`usage_api.py`, `settings_api.py`, `memory_api.py`,
  `conversation_api.py`), mounted with `app.include_router` beside
  `business_api`, `data_api` and `diagnostics_api`
- **Frontend**: Vite + TypeScript + Three.js (audio-reactive orb)
- **Communication**: WebSocket (JSON messages + binary audio)
- **AI**: a long-lived Claude Code process (`brain.py`, Sonnet by default) on
  the user's subscription; no Anthropic API calls on the voice path
- **TTS**: Fish Audio with JARVIS voice model
- **System**: AppleScript on macOS; isolated PowerShell, Winsock and console helpers on Windows
- **Lifecycle**: `process_tree.py` owns children; `run_store.transition_run` claims state changes atomically
- **Operations**: `conversation_store.py`, `diagnostics_api.py`, `data_api.py`, and `maintenance.py`; see `docs/operations.md`
- **Runs**: every Claude Code execution goes through one recorded pipeline —
  `run_store.py` (SQLite) + `run_executor.py`, surfaced at `/api/runs`,
  `/ws/runs`, and the `/dashboard` UI
- **Internal tool channel**: the brain reaches JARVIS's tools (session
  listing, steering, etc.) via a stdio MCP child that forwards
  `tools/call` to `POST /internal/tool` over loopback HTTP, bearing the
  token from `data_paths.ensure_tool_token()`. That token only ever goes
  straight to the host and port the URL names: never through HTTP(S)_PROXY
  — which `claude_env.child_env` passes through on purpose, and which
  urllib and httpx both apply to 127.0.0.1 unless NO_PROXY names it (on
  Windows, with none set, they read Internet Options) — and never on to a
  redirect, which urllib re-sends with the Authorization header on. The
  MCP child and the PreToolUse hook call through `loopback_http.py`
  (`http.client`, not urllib; a 3xx is a refusal); the ChatGPT fallback's
  gateway (`guarded_mcp.Gate`) asks `/internal/pretool` with its own
  `http.client` call under the same rules — a loopback origin only,
  anything but a 200 a refusal, any failure a refusal — so a change to
  one transport is a change to both; a script that uses httpx passes
  `trust_env=False` (`scripts/telegram_setup.py`). Held by
  tests/test_loopback_http.py, tests/test_pretool_hook.py,
  tests/test_jarvis_mcp.py and tests/test_guarded_mcp.py, with the
  stand-ins in tests/loopback_servers.py.
- **The owner's phone** (`messaging.py`, `telegram.py`, `whatsapp.py`):
  a second door. One policy in `messaging.py` — approval cards with buttons
  via `business_store.ON_PROPOSED`, needs-you / failure / completion lines
  when no tab is connected, voice notes as opus from `tts.py`, decisions
  through `business_api.decide` (the desk's own function, recorded as
  `via:<line>`), a phone turn (`server._phone_chat`), a read-back-and-`go`
  for anything that turn stages, and the `message_user` tool — over two
  transports: Telegram (`telegram.py`: a bot, `getUpdates` long polling,
  pairing by a six-digit code, `telegram_api.py`) and WhatsApp
  (`whatsapp.py`: Kapso's proxy of the Cloud API, a poll of its stored
  messages, the 24-hour-window template, `whatsapp_api.py`). Both poll
  rather than take a webhook, so the server stays loopback-only; both are
  owner-only both ways. A turn runs BESIDE the poll (`messaging._start_turn`,
  one at a time, in order) so a tap on a card that turn is holding is read
  while it holds it; a read-back is answerable only once delivered, on its
  own line (`server._phone_shown`). A FORWARDED message is somebody else's
  words: no word gate sees it, and `_phone_chat` walls it and starts the turn
  tainted (`Brain.turn(untrusted=…)`). The Telegram token is in every request
  URL, so `telegram.URL_LOGGERS` filter it out of the log. See
  `docs/telegram.md` and `docs/whatsapp.md`.
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

## The gate on a user's own MCP servers
A tool from a server the USER declared (`connections.json`) never reaches
`/internal/tool`: the CLI starts that server itself and calls it directly,
so the origin check, the acting-tool gate and the untrusted-content refusal
are all on a path it does not take. Measured live, the brain published a
LinkedIn post during a turn it had been told to rehearse first, and nothing
was in a position to stop it.

A **PreToolUse hook** is. Verified against CLI 2.1.270 rather than assumed:
`--dangerously-skip-permissions` removes the PROMPT, not the hook — a deny
still stops the call, the matcher matches MCP names, and the user's server
never runs the tool.

- `brain.settings()` installs it, matching `mcp__(?!jarvis__(?!_))`. JARVIS's
  own tools are excluded: already gated, and gating them here would
  deadlock. A tool of JARVIS's never begins with `_`, so `mcp__jarvis___x`
  (a server named `jarvis_`) is still gated.
- `pretool_hook.py` is a pipe and nothing else. It **fails closed** —
  including when the hook itself goes wrong: by Claude Code's hook contract
  an exit code other than 0 or 2 is a non-blocking error and the call
  PROCEEDS, so nothing past argument parsing may raise out of `main`, and
  its one non-stdlib import (`loopback_http`) is inside the guarded path,
  not at the top. It
  asks `/internal/pretool` straight, with no proxy and no redirect (see the
  internal tool channel above): a verdict from anywhere else is not
  JARVIS's. tests/test_pretool_hook.py runs it the way the CLI does.
- `pretool_gate.py` is the policy: reads pass, everything else is held. It
  matches VERBS, not tool names, so a server added next year is covered, and
  an unknown verb counts as outward — being wrong about a read costs one
  click, being wrong about a write costs a public post.
- The hook sends only the CLI's spelling, every symbol made `_`, so
  `get&delete` alone would read as `get_delete`'s one verb. The brain asks
  the CLI itself for each connector's OWN tool names — the `mcp_status`
  control request, after every init event and again as the model reaches
  for one of the user's tools (a server may re-spell one mid-turn; the ask
  races the hook's own process start, nothing waits on it) — and keeps them
  by process (`Brain.own_tool_names`). Verified against CLI 2.1.270: the answer names
  each tool as its server does, and has none before the servers connect;
  the init event and the hook payload carry only the CLI's spelling.
  `pretool_gate.classify_hook_call` reads every own name as the fallback's
  gateway's is read, one that acts holds the call, and a name with `_` in
  its tool part that no live process has reported is held. The answer
  echoes each server's `config` (headers, env): only names are read from it.
  A killed turn's account asks what the gate DID (`tool_log.held_since`),
  not the names, which went with its process.
- `/internal/pretool` decides, and stages a held call in the business
  desk's queue as `connector:<server>`, so the existing approval UI lists
  it. Approval is bound to the exact payload and **spent once**: approving
  one wording does not approve an edited one, and a second identical send
  needs a new card.
- **A card for bytes already sent says so** (`business_api.repeat_note`,
  over `business_store.sent_before`, deleted cards included): on the desk,
  on the phone card's first lines, in the voice and in the reason to the
  brain. Measured live, 2026-10-01: a confirmed post errored mid-typing,
  and three hours later the identical post was staged as a card that looked
  new, and approved. It is a warning, not a refusal — a retry after a real
  failure has to stay possible — so it carries what the first send did.
- **What a released call did is written on its card.** `PostToolUse` and
  `PostToolUseFailure` hooks (same matcher; `pretool_hook.py --report`, a
  pipe that never blocks) hand the result to `/internal/posttool`, which
  ties it back by `tool_use_id` to the gate's own `allow` row
  (`tool_log.allowed_call`) and writes `tool_outcome.summarise` onto that
  card's `result` (`business_store.record_outcome`; the card stays
  `submitted`, which only ever meant let through). A read that FOUND a
  post's link quoting a post this connector made in the last week writes
  the link onto that post's card. Verified against CLI 2.1.270: success
  carries the content blocks, an MCP error carries `error`. The fallback's
  gateway reports no outcome yet: a card it released says only `submitted`.
- **A connector's own `readOnlyHint` frees a call its name would hold**
  (`pretool_gate.frees_read_only`): `resolve_post_url` was a second card for
  a read, its wait lapsed, and the post's link was never found. The hint
  arrives with the own names in the CLI's `mcp_status` (`annotations.
  readOnly`), every live process must report it (`Brain.
  own_tools_read_only`), and it never frees a name that leads with an acting
  verb, has an always-acting word, or joins verbs. The model cannot write
  it; a server that lied about its own tool could act without asking
  anyway. The fallback's gateway does not send it, so ChatGPT's path stays
  the stricter one.
- A held call WAITS for the user rather than refusing and making him ask
  again. Verified against CLI 2.1.270: a hook given `timeout` in
  `--settings` blocks the call, so approval releases the ORIGINAL bytes —
  the brain never re-derives them, which is what makes a one-shot digest
  safe. Resuming by re-prompting would be a lottery against that digest:
  one re-worded sentence stages a second card and tells the brain again
  that nothing was sent, rebuilding the duplicate out of the machinery
  meant to prevent it.
- The budgets nest so the innermost gives up first: 120s for the wait,
  150s for the hook's HTTP call, 180s for the CLI's hook timeout, 300s for
  the turn. `tools_outstanding` keeps the silence watchdog off it.
- The server announces the wait itself, because the brain is blocked inside
  the call and cannot narrate it.
- The brain can READ what it is sending: `business_action` returns one card
  in full — its payload as JSON that parses to exactly the value the digest
  is over, paged rather than cut, secrets redacted and SAID to be — and
  `business_status` lists live cards first without payloads. Both are
  assembled under `TOOL_RESULT_CAP` rather than cut by it; a test re-issues
  a call from nothing but what `business_action` showed and the gate lets it
  through. Measured live:
  the only view of a card was the whole ledger in one string, the cap cut it
  inside the approved card, and JARVIS rightly refused three times to send a
  comment he could not see.
- **Every decision is written down** (`tool_log.py`), BEFORE the hook is
  told to proceed. Until this existed the only trace of any tool call was
  one `log.info` line to stderr with no file handler — gone with the
  scrollback. A gate that stops an action is half of it; the other half is
  a record that survives the process, which is what "he posted twice and
  nobody could tell" was missing. Bounded to 20,000 rows, pruned on every
  write so the cap is exact. Read at `GET /api/tool-calls`, by a browser
  and not by the brain: the loopback token authenticates the thing this
  record is ABOUT.
- The killed-turn clause asks that log, scoped to the turn's own window via
  `TurnResult.duration_sec`. Asking the unbounded question made a post
  submitted last week come back as something the turn had just done.

## LinkedIn: the limits, the stop, and the official API
Owner decision, 2026-10-08: LinkedIn's rules forbid automated posting through
a signed-in browser, which is what the `linkedin` connector does. User-facing:
`docs/linkedin.md`.

- `linkedin_guard.py` is the rate and the stop. At most 1 post a day per
  account, 6 hours apart, and 5 comments a day (`LINKEDIN_*` in `.env`; the
  brain cannot write them). Counted from the desk's ledger: connector cards
  let through and API cards sent, deleted or not, errored or not. A
  challenge, captcha, "unusual activity", restriction or failed sign-in in
  a LinkedIn call's OWN status (never its content: a feed post about a
  captcha is not one) halts every LinkedIn call, reads included, until
  `POST /api/linkedin/resume` from the desk. That route refuses the
  loopback token, so the brain cannot lift it.
- The gate enforces both (`server.internal_pretool`): halted LinkedIn
  connector calls are refused at the door; a publish over the limit is
  refused before any card is staged, and again before an approval is spent.
  An approval refused that way stays unspent. `/internal/posttool` is where
  a connector result trips the stop (`_linkedin_result_objects`).
- `linkedin_api.py` is the official API as the desk's `linkedin` provider:
  Posts API (`w_member_social`; `w_organization_social` for the company
  page, a separate Community Management app), Images and Videos APIs for
  media, `socialActions` comments. Approval sends the exact card once
  (`business_api.decide`). The limits and the stop are checked at
  `propose` and again in `_execute`. A lost answer to the publishing request is
  `unknown`; anything before it is a clean failure. 401, 429 and 999 halt;
  403 (a missing scope) only fails. `commentary` goes out escaped in
  LinkedIn's "little" format (`little`), with a `#` before a letter kept as
  a hashtag. Media comes only from `LINKEDIN_MEDIA_ROOTS`, bound to the
  card by sha256 and re-hashed before upload.
- The owner signs in himself: `/api/linkedin/connect` → LinkedIn →
  `/api/linkedin/callback` (a state issued here, spent once, ten minutes).
  Tokens are written to `<data>/linkedin-tokens.json`, never to a card, a
  receipt, a log or the repo. They last 60 days with no refresh token, and
  the desk shows the date.
- `linkedin_handpost.py` is the company page's route until its API access
  is granted: desk provider `linkedin_hand`. Approval publishes nothing. It
  sends the owner, on Telegram, the text as its own message and the media
  file as a document, and he posts it by hand. His Telegram REPLY to that
  delivery with the post's link is the say-so for writing it on the card
  (`telegram.handle` → `record_reply`, no brain turn). `Update.reply_to`
  carries which message a reply answers.

## The ChatGPT fallback
Opt-in (`JARVIS_CHATGPT_FALLBACK=1`): while Claude's usage limit holds, a
USER's turn goes to the Codex CLI on the owner's ChatGPT subscription, and
the first user turn after the reset goes back to Claude. The design rule is
that it is a second brain with the SAME gates: everything it can do goes
through `/internal/tool` or `/internal/pretool`, and where a gate could not
be kept the capability is removed. User-facing: `docs/chatgpt-fallback.md`.

- `chatgpt_fallback.py` is the Codex half: the binary (executables only —
  never npm's `.cmd` shim, which cmd.exe would re-parse), JARVIS's own
  `CODEX_HOME` (`<data>/codex-home`, inside the read wall), a cwd outside
  any repository, the one command line (every override after `exec`; every
  tool-shaped feature `--disable`d, and `--strict-config`), the readiness
  check, and one turn. `model_provider`, `chatgpt_base_url` and
  `developer_instructions` are pinned to Codex's own defaults on every run
  (an override beats every layer; `developer_instructions=""` sends
  nothing, as unset does — measured). Readiness asks first what needs no
  Codex — no `[features]`/`profile(s)` in its home's config.toml, no
  machine-wide layer where Codex looks for one (`system_config_folders`:
  the ProgramData known folder, the environment's, `C:\ProgramData`), the
  cwd outside any project — and then Codex, locally and in that cwd: a
  ChatGPT login (an API key bills the key), `features list` with exec's
  `-c` settings, read strictly (every line understood — names may be
  dotted — every disabled feature seen off, nothing on outside
  `KEEP_FEATURES`; `unified_exec`, which no `--disable` switches off in
  this build, only while `shell_tool` is off: `INERT_WHILE_OFF`), and no
  code-mode model by `debug models` or a cache this version wrote (stamped
  without its pre-release part, compared exactly). A bare name is resolved
  on PATH before it runs (`_resolved`), so it is stamped; an ok that could
  not be stamped is not taken as current. A refusal that ran no Codex
  (`Readiness.static`: switched off, a configuration file, the folder) is
  asked again every time rather than remembered. The machine-wide check
  runs again right before every turn (`config_problem`) and after it — a
  file there after the run, or any change to the folders while it ran
  (`config_fingerprint`: a file there only while Codex started; on Windows
  the `OpenAI` folder above too — one who owns the folder can still reset
  its times, which only an ACL would stop, the owner's call), is a
  breach, the answer is set aside, and later refusals say so
  (`CONFIG_APPEARED`, `note_breach(reason=, remedy=)`): `openai_base_url`
  cannot be pinned.
  `tests/fixtures/codex_features_0.155.0-alpha.9.2.txt` is the real table,
  captured with the flags beside it (`.flags.txt`), and passes. A run that
  reports an item outside `ALLOWED_ITEMS`, calls a tool on a server not in
  its own `mcp_servers` table, or has one of Codex's own resource readers
  answered by such a server, is killed and the fallback stays off until
  restart (`note_breach`, which records the call). A reader is only an
  item of the measured shape (`reader_call`: one of the two listings as
  server "codex" naming no server — whatever else it carries, since its
  answer is checked — or exactly the server named, trimmed as Codex trims
  it), so a same-named tool on another
  server is that server's; an all-server listing whose answer names a
  server outside the table, or is not exactly a listing, is a breach
  (`listed_servers`: none of JARVIS's servers has resources). A refused
  setting or a model its run marks code-mode holds it off for the rest of
  the limit (`mark_unready` / `_held`). A check already in flight never
  overwrites either (`_epoch`); `cached_readiness` never runs one, nor
  enters `readiness`; a turn queued behind one decides again under the
  lock, where a binary updated in place is vetted again (`readiness` asks
  `is_current`). Codex's per-call timeout outlasts the turn ceiling, so it
  never abandons a call the service may still complete. Measured against
  codex-cli 0.155.0-alpha.9.2.
- `guarded_mcp.py` stands in front of each of the user's servers: six
  methods through, every `tools/call` put to `/internal/pretool` with the
  turn's nonce and the Claude CLI's own name for the tool
  (`claude_env.mcp_tool_name`: `mcp__${cn(server)}__${cn(tool)}`, verified
  in the CLI binary, per UTF-16 code unit), plus the tool's own name so the
  gate reads `get&delete` as two verbs (`pretool_gate.classify_call`: only
  `& + | , ;` join verbs; `.`, `/` and the rest separate words, as the
  CLI's spelling does); re-serialised from what was gated. The Claude
  path's hook sends only the CLI's spelling, and the gate reads the own
  names the brain heard from the CLI's `mcp_status` instead
  (`pretool_gate.classify_hook_call`; "The gate on a user's own MCP
  servers"), so both brains get one verdict. Codex is never given a user's
  server directly. A server name that would split somewhere else
  (`claude_env.server_name_problem`: `jarvis_`, `jarvis.`), or that the
  CLI would write the same as another, is refused here and in
  `declared_connections` alike. An answer the gateway cannot read — on
  either connector, in bytes that are not UTF-8 too — is answered as
  garbled when its OUTERMOST `id` says which request it is (never a nested
  one, never a server request's), never left to time out.
- `brain.py` routes. Only `origin == "user"` falls back; a turn Claude
  refused for the limit falls back only if it ran no tool and said nothing.
  The fallback turn runs under `_turn_lock` and sets `_inflight`
  (`provider = "chatgpt"`), so the gates see its origin and taint. The Codex
  thread carries its OWN generation taint, reset only with the thread (a
  new limit, a new Claude generation, "start fresh"). What ChatGPT said is
  handed to Claude walled (`<session-output name="fallback">`), taken under
  the turn lock, and carries the thread's taint into Claude's generation;
  it is dropped only once Claude has answered with it. The switch is told
  through `on_switch` before ChatGPT's answer (after Claude's held answer,
  on the way back: the voice turn feeds it after `hold.finish()`), and only
  once ChatGPT has answered; "back on Claude" wherever "standing in" was
  said this episode (`_episode_announced`), and on a phone line only on a
  reply Claude gave, to a line whose own last news was "standing in" or
  the limit, whatever came and went while it was silent; a line whose
  ChatGPT turn failed before it heard is told the limit. A limit that begins while the episode is still
  open is a NEW limit — said again (`_announcement_owed`), readiness asked
  afresh — only when Claude has served a turn since the last one began or
  it reports a later reset (`_note_new_limit`); the same limit refused
  again in words once its minute's hold ran out is the same limit. A turn
  that waited on the lock past the reset is Claude's, on either path: a
  refused turn is asked again, at most once, through all of `turn()` — an
  owed fresh start, the limit, ChatGPT. A refusal the CLI reports only in words is still the limit
  (`_limit_from_error`) — the CLI's own words only (`TurnResult.cli_error`:
  its result string, its `errors`, or a message it wrote itself, `model:
  "<synthetic>"`), at their start, as the CLI's own list of limit messages
  begins; not its "not your usage limit" 429, not fast mode's limit, never
  the model's words; a reset more than a week off is not believed. A
  breach's notice names what Codex did; beside it, what the turn had done
  (`server._error_line`), never "nothing was changed". Two notices are
  both said.
- `/internal/pretool` taints a gateway call's turn at the gate, before the
  call is forwarded — ChatGPT's own report of it arrives later on its
  stream — and refuses a call whose nonce is not the turn in flight's,
  without ever spending an approval on one. `_caller_origin`: a call with
  a nonce is that nonce's turn's or nobody's, and during a ChatGPT turn a
  call without it has no origin — the idle Claude CLI cannot borrow the
  owner's turn; nor has a call before the CLI has echoed the Claude turn's
  message (`Brain.turn_answering`). Whose context a call's answer lands in
  is decided at the door too (`Brain.call_owner`): the turn in flight once
  echoed, the Claude process (`IdleClaude(proc)`, no nonce and no Claude
  turn answering — idle, beside a ChatGPT turn, or answering somebody else
  ahead of the turn's echo; mid-rotation, the predecessor held in reserve,
  `mid_rotation`, and marked against the successor's generation as well,
  since the call cannot be told apart from its warm-up), or nobody (a
  stopped Codex).
  `/internal/tool` marks what it read
  against that owner (`server._mark_read`, `Brain.mark_read_by`): a
  ChatGPT turn's own thread once it has ended, never a turn that did not
  make the call; and for the Claude process — idle, or a turn of it that
  has ended — that process's ONE conversation (`_mark_process_read`): its
  generation and the Claude turn now in flight on it, the reserved
  predecessor's during a rotation, nobody's once it is gone. An acting
  call whose turn ended during the await is refused. Every connector read
  the hook lets through is marked the same way (`_mark_hook_read`).
- A wake is told from a turn exactly (`Brain._handle`). Each of JARVIS's
  messages carries a fresh tag as its `uuid` (`_Turn.tag`), and the CLI,
  run with `--replay-user-messages`, echoes it back (`user`, `isReplay`,
  that `uuid`) as the message enters its conversation. Until that echo,
  whatever the process emits is a wake's (`_wake_event`): not heard, no
  `on_delta`, not the turn's tools, its calls nobody's, and a `result`
  before the echo ends the wake, not the turn — unless the result names
  the tag (`user_message_uuid(s)`), which is how a turn that fails before
  its first output, and is never echoed, ends. From the echo, the turn's,
  to the next `result`. Its init and `rate_limit_event` are the process's,
  read whenever they come. A wake — a message JARVIS did not write echoed,
  or the model's own output with no turn of JARVIS's answering, from a
  process serving, warming up (`_warming`) or held in reserve, not one
  being torn down — has read something JARVIS did not write
  (`IDLE_WAKE_SOURCE`), and what it reaches for is marked too: against the
  process's conversation and the turn waiting on it, which answers with
  it in front of it. So is a turn whose message the CLI folded into a
  wake's turn (echoed mid-wake), and one another session's message was
  folded into (a foreign echo after the turn's own). A wake's calls out
  hold a queued turn's silence clock (`_Wake`); the ceiling still bounds
  it. Measured on claude 2.1.270 (bundle, then the binary against a fake
  Messages API): docs/chatgpt-fallback.md, "Telling a wake from a turn";
  tests/test_wake_echo.py, on tests/fixtures/fake_brain.py, which queues,
  folds and echoes as the CLI does.
- A "start fresh" carries nothing forward, on either path: no note in
  process, and a `fresh-start` entry in the journal
  (`jarvis_memory.FRESH_START_REASON`) that `latest_journal` — and the
  dashboard's `latest_journal_slug` — never reads past, so no later start,
  crash restart or JARVIS restart, reads a note from before it; notes
  written after it are read as usual. Only JARVIS writes one: the brain's
  `write_journal` tool files a placeholder or wall reason as "manual",
  judged on the name it would be filed under (`unreserved_reason`).
  Every explicit request walls, even while an earlier one is still owed.
  Taken back if the Claude-path rotation does not happen; a rotation whose
  predecessor died counts as done. Said while ChatGPT stands in, the note
  is let go of and the wall written at once
  (`forget_fallback_conversation(fresh=True)`: the next ChatGPT thread's
  persona and any restart read neither), and the rotation is owed to
  Claude, set before anything waits: until it (settled once, by the next
  user turn after the reset; a second waits on `_fresh_settling`, one
  another rotation carried out is not done again — `rotate(only_if_owed=
  True)` — nor one a restart launched after the request took hold
  (`_fresh_asked`/`_fresh_applied`), and it writes no second wall over a
  note written meanwhile), a system turn gets `not_running`, so the
  discarded generation writes no handover. A settle that fails says so
  (`FRESH_NOT_CLEARED`), keeps the hand-back for the generation that
  replaces it, and survives a cancelled turn. `_start_fresh` returns the
  line it earned, which a phone line replies with.
- A warm-up refused while Claude's limit holds is not a crash: the restart
  loop takes the try back and waits for the reset, and a refusal holds the
  limit at least `LIMIT_MIN_HOLD_SEC` whatever reset it reports; a try
  taken back is not announced again. `_maybe_rotate` does nothing while the
  limit holds or the brain is not serving, keeps nothing from a journal
  turn that met the limit, and asks `rotate` to check again under its own
  locks that the generation it meant is still serving and still owes a
  rotation (`expected_generation`, `only_if_pending`); the banner comes
  back however it ends. The `rate_limited` event is never spoken: the turn
  the limit ends says it, with what the turn had done (`_limit_reply`).
- No test runs the real Codex: `tests/fixtures/fake_codex.py` speaks its
  measured JSONL, and conftest's `_never_run_the_real_codex` finds nothing
  on the machine outside `test_chatgpt_fallback.py`, whose candidate test
  points the search at directories of its own. Nor does any test read this
  machine's own machine-wide Codex layer (`system_config_folders` points at
  a temporary folder; `real_system_config_folders` is the real search).

## Changing a business record by voice
`business_record` is a durable writer: refused for the rest of a brain
generation that has read anything JARVIS did not write. `business_status`
is such a read (record text can be pasted from anywhere) and used to be the
only source of an update's id and version, so no update could ever land.

- `business_find` (business_api.py) matches the USER'S words against titles
  and contacts in the server and returns **handles** — `record_handle`: id,
  version, kind, status, due, amount, currency, save time, each re-checked
  against the closed set or shape `Record` admits. It is in
  `TAINT_EXEMPT_TOOLS` on exactly that ground, so **never add a title,
  notes, contact or any other free text to a handle**.
- `business_record` replies with the same handle, never the saved row — an
  update keeps stored notes the brain never saw — and a refused update names
  the field, never the value (`_invalid`), because pydantic quotes the
  merged record.
- Both fail only in `Refused` sentences written in business_api
  (`_worded_here`): an exception's own text can quote the row it choked on
  (sqlite3's "Could not decode … with text '…'"). The store reads record
  rows as bytes and judges them itself (`business_store._readable`), and
  `/internal/tool` marks a TAINTING tool's taint when it fails as well as
  when it returns.
- The gate itself is unchanged. tests/test_business_find.py holds all of it
  with an instruction planted in every column.

`approve_document` is the other durable writer, and the same rule holds: the
brain names what it approves in words the server resolves, never in
something it had to read. No tool hands it a document's path, so it passes
`kind` ("spec" / "plan") and gets the newest of that kind; with neither kind
nor path it gets the one document still awaiting approval, or a question if
both are. The default used to be the newest file, which mid-build is always
the plan. tests/test_approve_after_review.py.

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
  user's Claude subscription, fed over stdin as stream-json, each message
  tagged and echoed back so a wake is never taken for a turn.
  `scripts/probe_cli_echo.py` checks the echo against the installed CLI —
  rerun it after a CLI upgrade
- `jarvis_mcp.py` — Stdio MCP server exposing JARVIS's tools to the brain;
  forwards `tools/call` to `POST /internal/tool`
- `loopback_http.py` — The transport the MCP child and the hook use for a
  call that carries the tool token: straight to JARVIS, no proxy, no
  redirect (the fallback's gateway in `guarded_mcp.py` has its own, under
  the same rules)
- `chatgpt_fallback.py` — The ChatGPT fallback's Codex half: binary, home,
  command line, readiness, one turn (see "The ChatGPT fallback")
- `guarded_mcp.py` — The fallback's gateway in front of each user MCP
  server; every tool call through `/internal/pretool`
- `scripts/chatgpt_setup.py` — `login` / `status` / `logout` for the
  fallback's Codex, in JARVIS's own Codex home
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
  (process / conversation / project), and who is at the other end of each:
  its `origin`, read from the roster's `entrypoint` through the CLI's own
  full list. A session a PROGRAM started (`sdk-ts`, `sdk-py`, `sdk-cli`,
  `mcp`, …) takes its prompts to that program, which answers them itself —
  Paperclip in under three seconds, measured — so it stays `working`
  (`waiting_on_host`) and becomes `needs_you` only past
  `HOST_ANSWER_GRACE_SEC`. Every sentence about a waiting prompt says where
  it is (`server._PROMPT_SHOWN`); "keystroke" only for a terminal.
  tests/test_prompt_owner.py. A conversation is SAID by its thread's own
  name — the roster's `name`, which the Claude desktop app sets to the
  sidebar title — cut to what `server._said_name` admits; only when that is
  the CLI's own (`nameSource: "derived"`, "chitauri-67") is it named by its
  folder. Desktop threads with no folder all run in one
  `scratch-2026-…-xxxxxx`, so the folder named them "the newest" and "the
  second" of it. And said AS a thread's name: "The thread “Jarvis tread
  name update” has finished, sir." — said bare, a title was heard as news
  about an update. `SessionState.thread_part` is the words of the voice name
  that are the thread's own, `server._said_name` marks them and
  `_sentence_start` capitalises only JARVIS's own article; the voice name
  itself, which is matched when the user says it back, is unchanged.
  tests/test_thread_names.py
- `session_steer.py` — Sends a message into a running session's inbox socket
- `dialog.py` — Answers a permission prompt in a Terminal window by sending
  it a keystroke (needs Accessibility)
- `notifier.py` — macOS notification fallback when no browser tab is
  connected to speak through
- `messaging.py` — The owner's phone, the half every line shares: the
  callbacks the server registers, the line registry and fan-out (`reach`,
  `say`, `notify_card`), `card_text` / `button_ids` / `parse_button`, the
  `approve` / `reject` and `go` / `cancel` word gates, and the inbound
  policy a transport hands an owner's message to (`on_text`, `on_button`,
  `on_other`), every decision through `business_api.decide`. Imports the
  transports lazily; they import it
- `telegram.py` — The Telegram line: config, the Bot API client, `getUpdates`
  long polling with a persisted offset and a per-update claim, `deleteWebhook`
  at start, inline-keyboard cards whose 64-byte buttons carry sixteen
  characters of the digest, buttons stripped once a card is decided,
  `sendVoice` for opus, and pairing (a six-digit code shown on the local
  Settings page and never in the status GET; whoever sends it becomes the
  owner, replacing any owner; while it is live every six-digit private
  message is an attempt, the owner's too, and one dated before the code was
  made neither pairs nor counts; withdrawn after five wrong ones; each code
  has a serial, an outcome and a note, which is what the page and the script
  wait on). A queued turn from a phone unpaired or replaced meanwhile is
  dropped (`owner_key`). `telegram_api.py` is its status, pair, pair/cancel,
  unpair and test routes; `scripts/telegram_setup.py` pairs from the
  terminal (with the server down, `poll_once(pairing_only=True)`)
- `whatsapp.py` — The WhatsApp line: Kapso client (owner-only sends, buttons,
  voice notes, template fallback for the shut 24-hour window), the inbound
  parser, the poller. `whatsapp_api.py` is its status and test routes;
  `scripts/whatsapp_setup.py` finds the number id and creates the template
- `jarvis_memory.py` — Long-term memory: a folder of plain Markdown files the
  user can read and edit directly, not a database. `unindexed_memories` /
  `reindex` are the report and the repair for a note MEMORY.md does not name
  (preflight, the dashboard, `maintenance.py reindex`); `project_history`
  is the read half of `project_note`. The brain home also holds `LOCAL.md`,
  the user's standing orders: seeded once by `data_paths`, `@`-imported by
  the shipped CLAUDE.md, never written by JARVIS
- `usage_store.py` — Tracks the subscription's five-hour / seven-day
  rate-limit usage (there is no spend to report — see brain.py)
- `preflight.py` — First-run environment checks: `claude` CLI/login,
  Accessibility, Fish key, cross-session steering, and whether JARVIS was
  itself started from inside a Claude Code session (`claude_session_env`)
- `claude_env.py` — The environment every spawned Claude Code child gets:
  the `ANTHROPIC_*` scrub, and everything a launching Claude Code session
  hands down (`CLAUDE_*`, `MCP_*`, `OTEL_*` and a few named keys), bar
  `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_GIT_BASH_PATH`. `python claude_env.py
  --inherited` names what to clear; `scripts/start-jarvis.ps1` asks it.
  `scripts/probe_child_env.py` measures which variables a real `claude -p`
  acts on — rerun it after a CLI upgrade
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
- `data_paths.py` — Single source of truth for where data is written, and
  for keeping it private: `restrict_to_owner` (icacls on Windows, chmod
  elsewhere, with a rollback if it would lock the running account out)
- `envfile.py` — The `.env` parser and its one load into `os.environ`;
  shared by `server.py` and `settings_api.py` so the writer checks values
  against the same reader
- `schema.py` — `ensure_columns`: the one PRAGMA-then-ALTER for a column
  added after a release shipped; every store uses it
- `usage_api.py`, `settings_api.py`, `memory_api.py`, `conversation_api.py`
  — the HTTP routers carved out of `server.py`; `server` re-imports the few
  names the voice path and the tests reach through it
- `maintenance.py` — Backup, verify, restore, export, `prune` (one
  retention policy over runs, conversation, journal, usage log and,
  opt-in, the brain's own transcripts) and `reindex`
- `frontend/src/panelwindow.ts`, `frontend/src/uibits.ts` — The voice
  page's shared window behaviour (drag, minimise, arrow keys, memory) and
  DOM pieces (`el`, the armed two-click button, `<time>`); the Conversation
  panel and the Business desk both use them

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
- `CLAUDE_*`, `MCP_*`, `OTEL_*` — **never passed to a Claude Code child**,
  bar `CLAUDE_CONFIG_DIR` (where the login is) and `CLAUDE_CODE_GIT_BASH_PATH`
  (where bash is). A Claude Code session exports these to everything it
  starts, and the CLI acts on several (`CLAUDE_CODE_EFFORT_LEVEL` overrides
  `--effort`, `API_TIMEOUT_MS` its timeout), so a JARVIS started from inside
  one would otherwise run its brain on that session's settings. To give an
  MCP server a variable, put it in that server's `env` in `connections.json`
- `JARVIS_BRAIN_MODEL` (optional, default `sonnet`) — model for the brain,
  always passed explicitly as `--model`
- `JARVIS_BRAIN_TURN_TIMEOUT` (optional, default `90`) — how long a turn may
  be SILENT before the brain is judged stuck, killed and restarted. Not how
  long a turn may take: a turn streaming text or waiting on a tool is alive
  however long it runs. Measured live, this was elapsed time and a healthy
  LinkedIn call (180s by that server's own budget) got the brain killed
  mid-turn.
- `JARVIS_BRAIN_TURN_CEILING` (optional, default `300`) — the backstop under
  that hold. While a tool is outstanding the silence budget does not apply, so
  this bounds the whole turn; without it a wedged tool holds `_turn_lock`
  forever and rotation can never run.
- `JARVIS_CHATGPT_FALLBACK` (optional, default off) — `1` lets ChatGPT, via
  the Codex CLI, stand in while Claude's limit holds.
  `JARVIS_CHATGPT_MODEL` (default `gpt-5.5`; a code-mode model is refused)
  and `JARVIS_CODEX_PATH` (a `codex.exe`; found by default). Every
  `OPENAI_*` and `CODEX_*` variable is scrubbed from Codex's environment
  and `CODEX_HOME` is always JARVIS's own. See `docs/chatgpt-fallback.md`
- `JARVIS_BRAIN_AUTOSTART` (optional, default `1`) — `0` builds the brain but
  never spawns it (every test sets this)
- `JARVIS_MUTE_MIC_DURING_SPEECH` (optional, default false) — fallback if echo
  rejection is not enough with a given microphone
- `TELEGRAM_BOT_TOKEN` (optional) and `TELEGRAM_OWNER_ID` (written by pairing,
  or set by hand) — the Telegram line; `TELEGRAM_APPROVALS`,
  `TELEGRAM_VOICE_NOTES` (default on), `TELEGRAM_API_BASE_URL`. Every
  `TELEGRAM_*` variable is scrubbed from Claude Code children. See
  `docs/telegram.md`
- `KAPSO_API_KEY`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_OWNER_NUMBER` (all
  optional, all three needed together) — the WhatsApp line. The owner's
  number falls back to `JARVIS_OWNER_PHONE`. `WHATSAPP_TEMPLATE` (an
  approved utility template with one named body parameter `message`) is
  what gets through once the 24-hour window is shut; `WHATSAPP_INBOUND`,
  `WHATSAPP_APPROVALS`, `WHATSAPP_VOICE_NOTES` (all default on),
  `WHATSAPP_POLL_HOT_SECONDS` / `WHATSAPP_POLL_IDLE_SECONDS` (3 / 20),
  `WHATSAPP_OWNER_WA_ID`, `KAPSO_API_BASE_URL`. Every `KAPSO_*` and
  `WHATSAPP_*` variable is scrubbed from Claude Code children. See
  `docs/whatsapp.md`
- `LINKEDIN_CLIENT_ID` / `LINKEDIN_CLIENT_SECRET` (the member app) and
  `LINKEDIN_ORG_CLIENT_ID` / `LINKEDIN_ORG_CLIENT_SECRET` /
  `LINKEDIN_ORGANIZATION_ID` (the company page's Community Management app),
  all optional — the official API (`linkedin_api.py`). `LINKEDIN_MEDIA_ROOTS`
  (where post media may come from), `LINKEDIN_POSTS_PER_DAY` /
  `LINKEDIN_COMMENTS_PER_DAY` / `LINKEDIN_MIN_POST_GAP_HOURS` (1 / 5 / 6,
  `linkedin_guard.py`), `LINKEDIN_CONNECTOR_SERVERS`, `LINKEDIN_API_VERSION`,
  `LINKEDIN_REDIRECT_URI`. The backend alone reads them, and every
  `LINKEDIN_*` variable is scrubbed from Claude Code children. That includes
  the browser connector's own knobs (`LINKEDIN_TRACE_MODE`,
  `LINKEDIN_DEBUG_*`, `LINKEDIN_MCP_*`): set those in the `linkedin` entry's
  `env` in `connections.json`, which both brains still pass it. See
  `docs/linkedin.md`
- `FISH_API_KEY` (required) — Fish Audio TTS
- `FISH_VOICE_ID` (optional) — Voice model ID
- `USER_NAME` (optional) — Your name for JARVIS to use
- `JARVIS_DATA_DIR` (optional) — Where JARVIS writes everything: the SQLite
  database, memory Markdown, usage.json, usage_log.jsonl, the tool token.
  Defaults to `data/` (`data_paths.py`). The brain's Claude Code transcripts
  are the one thing outside it (the CLI writes them under its own config
  dir); `repo_read` treats that directory as a private root. Set it to run an isolated instance without touching live
  data — the test suite gives every test a fresh one this way.
- `JARVIS_SKIP_PERMISSIONS` (optional) — Defaults to true; passes
  `--dangerously-skip-permissions` to spawned runs, which have no TTY to
  answer a permission prompt
- `WEATHER_LOCATION_LABEL` / `WEATHER_LATITUDE` / `WEATHER_LONGITUDE` /
  `WEATHER_UNIT` (all optional) — override the auto-detected (public-IP)
  weather location and units
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
- SQLite for runs, run events and usage (`run_store.py`, `usage_store.py`);
  long-term memory is plain Markdown files instead (`jarvis_memory.py`) —
  the user edits it directly, so it is never a database
