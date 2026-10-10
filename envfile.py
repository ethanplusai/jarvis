"""The repository's `.env`: how it is read, and loading it once at import.

Read by two things that must not import each other — `server.py`, before it
defines anything (its constants read `os.environ`), and `settings_api.py`,
which writes the file back and checks every value round-trips through the
SAME parser before it commits it. So the parser is a leaf module, and the
load happens on first import of that module, with `setdefault`: a variable
already in the environment wins over the file.
"""
import os
from pathlib import Path

ENV_PATH = Path(__file__).parent / ".env"


def parse_env_lines(text: str) -> list[tuple[str, str]]:
    """Every (key, value) a reader of `.env` sees in `text`, in order."""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out.append((k.strip(), v.strip().strip('"').strip("'")))
    return out


def load_once() -> None:
    """Put `.env` into `os.environ` for anything not already set there."""
    if ENV_PATH.exists():
        for key, value in parse_env_lines(ENV_PATH.read_text(encoding="utf-8")):
            os.environ.setdefault(key, value)


load_once()
