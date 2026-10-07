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
    r"""The argv for a configured `claude` command, which may carry arguments.

    A path to an existing file is taken whole: POSIX `shlex.split` reads the
    backslashes in a Windows path (`C:\Users\...\claude.exe`, exactly what
    `shutil.which` returns there) as escapes and drops them, leaving a path
    that does not exist."""
    if os.path.isfile(command):
        return [command]
    return shlex.split(command)
