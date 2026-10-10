"""The environment every Claude Code child of JARVIS is given.

There is exactly one rule and it is a billing rule: JARVIS's Claude Code
children run on the user's **subscription**, never on an API key. The CLI
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
"""

from __future__ import annotations

import os
import shlex
import sys

# Every ANTHROPIC_* variable, not just the key: the base URL and the model
# override redirect a child just as effectively as credentials do.
SCRUBBED_ENV_PREFIXES = ("CLAUDE_CODE_", "ANTHROPIC_")
SCRUBBED_ENV_KEYS = {"CLAUDECODE"}

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


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment with everything that would redirect or
    re-bill a Claude Code child removed. Everything else — PATH, HOME,
    CLAUDE_CONFIG_DIR, the user's own variables — passes through untouched."""
    source = os.environ if base is None else base
    return {k: v for k, v in source.items()
            if not k.startswith(SCRUBBED_ENV_PREFIXES)
            and k not in SCRUBBED_ENV_KEYS}


def split_command(command: str) -> list[str]:
    """Turn the configured `claude` command into argv.

    It is usually a bare path, but tests and power users pass a prefix such as
    `python fake_brain.py`. POSIX shlex treats a backslash as an escape, which
    turns a Windows path such as `C:/Users/me/claude.exe` written with
    backslashes into `C:Usersmeclaude.exe`, so on
    Windows the command is split the cmd.exe way and its quotes stripped. A
    command that names an existing file is never split, so a path with spaces
    survives on either platform.
    """
    if os.path.isfile(command):
        return [command]
    if sys.platform != "win32":
        return shlex.split(command)
    return [part[1:-1] if len(part) >= 2 and part[0] == part[-1] == '"' else part
            for part in shlex.split(command, posix=False)]


def _kill_descendants(proc) -> None:
    """Windows: kill everything `proc` started. TerminateProcess ends the one
    process it is given, so claude.exe's tool shells and MCP servers would
    outlive it, still holding the project directory. POSIX needs none of
    this: the children are reaped through their own pipes closing."""
    if sys.platform != "win32":
        return
    try:
        import psutil
        children = psutil.Process(proc.pid).children(recursive=True)
    except Exception:
        return
    for child in children:
        try:
            child.kill()
        except Exception:
            pass


def terminate(proc) -> None:
    """Ask a Claude Code child to stop: SIGTERM, or on Windows its whole tree."""
    _kill_descendants(proc)
    proc.terminate()


def kill(proc) -> None:
    """Stop a Claude Code child now: SIGKILL, or on Windows its whole tree."""
    _kill_descendants(proc)
    proc.kill()
