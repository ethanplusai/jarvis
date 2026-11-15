"""
untrusted_text.py — wrap text JARVIS did not write, for the stdio MCP servers.

`mac_mcp.py` and `briefing_mcp.py` read words other people chose: a window on
the screen (a web page, an email), a shared note, a calendar invitation's
title, a news headline. The turn is already tainted for them
(`brain.untrusted_tool_source` marks every non-`jarvis` MCP tool), so the
acting tools are shut. This adds the other half: the text itself arrives
inside the same `<session-output … untrusted="true">` block that
`server._wrap_untrusted` uses, so the brain sees where the foreign words
start and stop and reads instructions inside as content to report.

It mirrors `server._wrap_untrusted` instead of importing it, because these
servers are small standalone processes and must not import server.py. The
format and the escaping must stay the same; `tests/test_untrusted_text.py`
checks that they do.
"""
from __future__ import annotations

import re

CAP = 3000
_NAME_SHAPE = re.compile(r"[a-z][a-z ]{0,23}")
_FALLBACK = "untrusted content"
_OPEN = re.compile(r"<session-output", re.IGNORECASE)
_CLOSE = re.compile(r"</session-output>", re.IGNORECASE)


def _break(m: re.Match) -> str:
    # A non-breaking hyphen makes the delimiter unparseable but still readable.
    return m.group(0).replace("-", "‑")


def wrap(name: str, text: str, cap: int = CAP) -> str:
    """`text` inside one closed, escaped untrusted block named `name`.

    `name` must be a literal from the caller, never something read from
    outside: it is written raw into the opening tag."""
    text = text or ""
    if len(text) > cap:
        text = text[:cap].rstrip() + "\n… (truncated)"
    if not _NAME_SHAPE.fullmatch(name or ""):
        name = _FALLBACK
    safe = _CLOSE.sub(_break, _OPEN.sub(_break, text))
    return (f'<session-output name="{name}" untrusted="true">\n{safe}\n'
            f'</session-output>')
