"""Shared sanitization utilities for JARVIS.

All AppleScript string interpolation and shell command construction
MUST use these functions. Never hand-roll escaping.
"""

import os
import shlex
from pathlib import Path

# Load .env if not already loaded (ensures config works regardless of import order)
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))


def escape_applescript(s: str) -> str:
    """Escape a string for safe embedding inside AppleScript double quotes.

    Order matters: backslashes first, then double quotes.
    """
    return s.replace("\\", "\\\\").replace('"', '\\"')


def escape_shell_in_applescript(path: str) -> str:
    """Escape a path for use inside a shell command inside AppleScript.

    Two layers: shell-quote with shlex, then AppleScript-escape.
    Use this for `do script "cd <path> && ..."` patterns.
    """
    return escape_applescript(shlex.quote(path))


# ---------------------------------------------------------------------------
# Config flags
# ---------------------------------------------------------------------------

ALLOW_DANGEROUS_PERMS = os.getenv("ALLOW_DANGEROUS_PERMISSIONS", "false").lower() == "true"
DANGEROUS_FLAG = " --dangerously-skip-permissions" if ALLOW_DANGEROUS_PERMS else ""
DANGEROUS_FLAG_LIST = ["--dangerously-skip-permissions"] if ALLOW_DANGEROUS_PERMS else []
