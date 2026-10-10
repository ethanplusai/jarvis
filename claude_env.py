"""The environment every Claude Code child of JARVIS is given.

The first rule is a billing rule: JARVIS's Claude Code children run on the
user's **subscription**, never on an API key. The CLI
prefers an inherited `ANTHROPIC_API_KEY` over the login without saying so —
`claude auth status` still reports `loggedIn: true` while `apiKeySource`
quietly flips to the env key and the account's email and organisation go
blank — so a key in the environment silently moves every spawned run onto
paid API billing.

`server.py` loads `.env` into `os.environ` at import, and a developer's
`.env` legitimately holds `ANTHROPIC_API_KEY` for the older lookup paths.
That is how the key reaches a child that never wanted it.

This module exists so the scrub is written once. It was fixed for the brain
during milestone 1 and NOT for the run pipeline, which is precisely the
failure mode a second copy of the logic produces: the copy that was not
updated is the one nobody notices.

The second rule is about whoever STARTED JARVIS. A Claude Code session hands
its own identity, wiring and tuning to every process it starts, and the
desktop app does the same for the sessions it hosts. Measured 2026-09-26:
JARVIS started by an agent (`scripts/start-jarvis.ps1` from a Claude Code
terminal) gave the brain's `claude -p` that session's CLAUDE_EFFORT,
CLAUDE_PID, CLAUDE_AGENT_SDK_VERSION, MCP_CONNECTION_NONBLOCKING,
MCP_SERVER_CONNECTION_BATCH_SIZE, API_TIMEOUT_MS, trace context and more —
the scrub removed only CLAUDE_CODE_* and ANTHROPIC_*. What the CLI does
with each was measured, not assumed, against CLI 2.1.270 and 2.1.280 with
`scripts/probe_child_env.py` (a real `claude -p` against a local fake API):

  * acted on: CLAUDE_CODE_EFFORT_LEVEL overrides `--effort` (low became
    high); CLAUDE_CODE_ENTRYPOINT and CLAUDE_AGENT_SDK_VERSION rewrite the
    User-Agent to claim the desktop app or the Agent SDK; API_TIMEOUT_MS
    replaces the API timeout (at 1500 ms, a turn the API answered in 4 s
    never finished: it retried until killed);
    MCP_SERVER_CONNECTION_BATCH_SIZE sets how many MCP servers start at
    once; OTEL_* send telemetry to the inherited collector as soon as
    telemetry is on; USE_STAGING_OAUTH and
    USE_LOCAL_OAUTH point the Claude in Chrome bridge at staging or at
    ws://localhost:8765 (read in the CLI's code, not exercised).
  * ignored: CLAUDE_EFFORT and CLAUDE_PID, which the CLI writes for its
    own children rather than reads (the `--effort low` brain's Bash child
    saw CLAUDE_EFFORT=low with xhigh inherited), AI_AGENT, which it
    replaces while it names a Claude Code agent (any other value it keeps
    and passes on), and
    MCP_CONNECTION_NONBLOCKING, which `-p` does not consult: the first turn
    waited for a slow server whatever it said. They are removed all the
    same: they describe another process, and the next CLI may read them.

So the rule is by namespace, not by the list that happened to be measured:
every CLAUDE_*, MCP_* and OTEL_* goes, and the few that are not session
state are named in `KEPT_ENV_KEYS`.
"""

from __future__ import annotations

import os
import re
import shlex

# What whoever started JARVIS handed down. JARVIS reads none of it itself
# (`session_steer` reads CLAUDE_CODE_MESSAGING_TOKEN, which authenticates
# only to the inbox of the session that exported it; see
# `preflight._check_claude_session_env_sync`).
#
# Every ANTHROPIC_* variable, not just the key: the base URL and the model
# override redirect a child just as effectively as credentials do.
INHERITED_ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "MCP_", "OTEL_")
INHERITED_ENV_KEYS = frozenset({
    "CLAUDECODE",           # "this process runs under Claude Code"
    "AI_AGENT",             # the identity of the agent that started it
    "API_TIMEOUT_MS",       # the host's API timeout, not the CLI's
    "DISABLE_MICROCOMPACT",  # the desktop app's setting for its own sessions
    "USE_LOCAL_OAUTH", "USE_STAGING_OAUTH",   # where the Chrome bridge connects
    # The launcher's trace, so a child's spans would join someone else's.
    "TRACEPARENT", "TRACESTATE", "BAGGAGE", "SENTRY-TRACE",
})

# JARVIS's own secrets, for the business providers and his phone lines.
# The backend reads them; no Claude Code child ever does. The brain reaches
# the owner's phone only through the `message_user` tool, which goes to the
# owner and nobody else; it never holds a key or token that could go
# elsewhere.
#
# LINKEDIN_ by the whole prefix: the app secrets, ids, organization, limits
# and media roots are all read by the backend alone (`linkedin_api`,
# `linkedin_guard`). The browser connector reads LINKEDIN_TRACE_MODE,
# LINKEDIN_DEBUG_* and LINKEDIN_MCP_* knobs of its own; those belong in its
# entry's `env` in connections.json, which both brains still hand it.
PRIVATE_ENV_PREFIXES = ("GOOGLE_ADS_", "META_", "TWILIO_", "OPENAI_ADS_", "KAPSO_", "WHATSAPP_",
                        "TELEGRAM_", "LINKEDIN_")
PRIVATE_ENV_KEYS = frozenset({"JARVIS_OWNER_PHONE"})

# Facts about this machine that live under a scrubbed prefix. Where the
# login is: without it the child cannot find the subscription it must bill.
# CLAUDE_SECURESTORAGE_CONFIG_DIR is the same fact for the stored
# credentials: CLI 2.1.270 reads them from there when it is set and falls
# back to CLAUDE_CONFIG_DIR only when it is not, and its own spawners always
# set the two together, so keeping one and scrubbing the other would send a
# child to look for the login where it is not. Where bash is: without it a
# Windows run has no Bash tool ("BashTool will be unavailable"); the CLI
# falls back to its own search if the path is wrong.
KEPT_ENV_KEYS = frozenset({"CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR",
                           "CLAUDE_CODE_GIT_BASH_PATH"})

# Set only in a process a Claude Code session (or the app hosting one)
# started. The desktop app strips the first two from its own sessions for
# the same reason. A user's own MCP_TIMEOUT is scrubbed, but it is not one
# of these: it says nothing about who started JARVIS.
SESSION_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT",
                   "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID")

SCRUBBED_ENV_PREFIXES = INHERITED_ENV_PREFIXES + PRIVATE_ENV_PREFIXES
SCRUBBED_ENV_KEYS = INHERITED_ENV_KEYS | PRIVATE_ENV_KEYS

# asyncio.create_subprocess_exec gives a child's stdout/stderr StreamReader a
# 64 KiB *line* buffer by default (asyncio.streams._DEFAULT_LIMIT). `claude -p
# --output-format stream-json` emits one JSON object per line, and a single
# line carrying a large tool result (a big file read, a long assistant
# message, a large diff) routinely exceeds that. When it does, `readline()`
# raises `ValueError("Separator is not found, and chunk exceed the limit")` —
# which, uncaught, killed an otherwise-healthy run (run_executor.py) or the
# brain process (brain.py) outright. A run that had been working for 28
# minutes was recorded as `failed` in 0 seconds because of exactly this.
#
# This is a buffer *ceiling*, not a pre-allocation: asyncio grows the
# underlying bytearray as data arrives, so a generous limit costs nothing
# while idle. 64 MiB is comfortably larger than any single stream-json line
# JARVIS has observed in practice (the worst offenders are full-file Read
# results and large diffs, which top out in the low single-digit MiB) while
# staying small enough that even a runaway line cannot balloon memory
# unboundedly — pass this to every `create_subprocess_exec(..., limit=...)`
# that reads a Claude Code child's stdout/stderr.
STREAM_LINE_LIMIT = 64 * 1024 * 1024  # 64 MiB per line


def is_inherited(name: str) -> bool:
    """Whether `name` is something a launching session hands down."""
    return name not in KEPT_ENV_KEYS and (
        name.startswith(INHERITED_ENV_PREFIXES) or name in INHERITED_ENV_KEYS)


def is_scrubbed(name: str) -> bool:
    """Whether a Claude Code child of JARVIS is kept from inheriting `name`."""
    return (is_inherited(name) or name.startswith(PRIVATE_ENV_PREFIXES)
            or name in PRIVATE_ENV_KEYS)


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment with everything that would redirect,
    re-bill, reconfigure or impersonate a Claude Code child removed.
    Everything else — PATH, HOME, CLAUDE_CONFIG_DIR, proxies, the user's own
    variables — passes through untouched."""
    source = os.environ if base is None else base
    return {k: v for k, v in source.items() if not is_scrubbed(k)}


def inherited_names(base: dict[str, str] | None = None) -> list[str]:
    """The variables in `base` (default: this process) that a launching
    session handed down, by name. JARVIS's own secrets are not among them:
    the backend needs those, and this is the list the launcher clears."""
    source = os.environ if base is None else base
    return sorted(k for k in source if is_inherited(k))


def session_markers(base: dict[str, str] | None = None) -> list[str]:
    """Which of `SESSION_MARKERS` are set: non-empty means a Claude Code
    session started this process."""
    source = os.environ if base is None else base
    return [k for k in SESSION_MARKERS if k in source]


def split_command(spec: str) -> list[str]:
    """`spec` as an argv prefix.

    `spec` is usually a bare path — what `shutil.which("claude")` returned, or
    `JARVIS_CLAUDE_PATH` — but it may also be a small command line ("node
    /path/to/cli.js"), so it is `shlex.split` on the way in. shlex's POSIX
    mode treats a backslash as an escape and strips it, which turns a Windows
    path like C:/Users/.../claude.EXE (with backslash separators) into one
    with no separators at all and every spawn into a FileNotFoundError. A
    spec that names an existing file is the whole argv already; only
    anything else is parsed.
    """
    if os.path.isfile(spec):
        return [spec]
    parts = shlex.split(spec, posix=(os.name != "nt"))
    # Non-POSIX shlex retains grouping quotes; CreateProcess must receive
    # unquoted argv entries so subprocess can quote paths exactly once.
    if os.name == "nt":
        parts = [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'"
                 else p for p in parts]
    return parts


# What the Claude CLI does to a server's or a tool's name before it becomes
# part of `mcp__<server>__<tool>`: every character outside [A-Za-z0-9_-] is
# replaced by `_`. `sub`, not a match: nothing here is a gate by itself.
_MCP_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
OWN_SERVER = "jarvis"


def mcp_name_part(name: str) -> str:
    """One half of an MCP tool's name, as the Claude CLI writes it — per
    UTF-16 code unit, as its JavaScript regex (no `u` flag) does: a
    character outside the Basic Multilingual Plane is two of them, and
    becomes `__`."""
    return _MCP_NAME_UNSAFE.sub(lambda m: "_" * (2 if ord(m.group(0)) > 0xFFFF else 1), name)


def mcp_tool_name(server: str, tool: str) -> str:
    """`mcp__<server>__<tool>` exactly as the Claude CLI names the tool.

    The ChatGPT fallback's gateway asks the gate with this name and its
    stream reader records tools by it, so one policy
    (`pretool_gate.classify`), one approval digest and one log cover both
    brains. With the raw name a dot could hide a verb from the policy, and a
    card approved on one brain was never spent by the other."""
    return f"mcp__{mcp_name_part(server)}__{mcp_name_part(tool)}"


def server_name_problem(name: str) -> str | None:
    """Why a declared server's name cannot be served, or None.

    Judged on the name as the CLI will write it, because that is the name
    every gate parses. A `__` inside it, or one at its end (`jarvis_`,
    `jarvis.`), makes `mcp__<server>__<tool>` split somewhere else —
    `mcp__jarvis___post` reads as JARVIS's own tool `_post`, which no gate
    holds. And `jarvis` itself is JARVIS's own."""
    written = mcp_name_part(name)
    if written == OWN_SERVER:
        return "reserved"
    if "__" in written or written.endswith("_"):
        return "ambiguous"
    return None


def pretool_hook_settings(tool_url_base: str) -> dict:
    """The PreToolUse gate, and the post-call reports beside it, as a
    `--settings` fragment.

    Here rather than in `brain.py` because a SPAWNED RUN needs exactly the
    same gate and imports this module too. It was in `brain.settings()`
    alone, which meant everything the gate protects covered one process:
    `RunExecutor._command` passed no `--settings` and no
    `--strict-mcp-config`, and `child_env` deliberately keeps HOME, so an
    unattended run loaded the USER'S own `~/.claude.json` servers under
    `--dangerously-skip-permissions`. Verified live against CLI 2.1.270:
    such a run reported `mcp__linkedin__send_message` in its own tool list.

    `mcp__jarvis__` is excluded: those are gated at `/internal/tool`
    already, and gating them here would deadlock — the gate's own
    bookkeeping goes through tools of his.

    `timeout` is what lets the gate HOLD a call open while the user
    decides. Outermost of four nested budgets; see
    `server.GATE_APPROVAL_WAIT_SEC`.
    """
    import sys
    from pathlib import Path

    import data_paths
    script = f'"{sys.executable}" "{Path(__file__).resolve().parent / "pretool_hook.py"}"'
    token = f' --token-file "{data_paths.tool_token_path()}"'
    hook = f'{script} --url "{tool_url_base}/internal/pretool"{token}'
    # What a released call came back with, written on its card
    # (`server.internal_posttool`). Verified against CLI 2.1.270: a success
    # fires PostToolUse with the call's content blocks, an MCP error fires
    # PostToolUseFailure with its text, both carrying the call's
    # `tool_use_id`. A report changes nothing about the call, so it is
    # short and a failure to deliver it costs only the record.
    report = f'{script} --report --url "{tool_url_base}/internal/posttool"{token}'
    # Not `mcp__jarvis___…`: a tool of JARVIS's never begins with `_`,
    # so that name is somebody else's, however it came to be written.
    matcher = r"mcp__(?!jarvis__(?!_))"
    after = [{"matcher": matcher, "hooks": [{"type": "command", "timeout": 30,
                                             "command": report}]}]
    return {"PreToolUse": [{"matcher": matcher,
                            "hooks": [{"type": "command", "timeout": 180,
                                       "command": hook}]}],
            "PostToolUse": after,
            "PostToolUseFailure": [dict(entry) for entry in after]}


if __name__ == "__main__":
    # `scripts/start-jarvis.ps1` asks this, rather than keeping a second copy
    # of the rule in PowerShell: one name per line, never a value, and
    # nothing at all when there is nothing to clear.
    import sys
    if sys.argv[1:] != ["--inherited"]:
        sys.exit("usage: python claude_env.py --inherited")
    for name in inherited_names():
        print(name)
