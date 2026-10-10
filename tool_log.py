"""What the PreToolUse gate decided, written down before the call resolves.

Until this existed, the only trace of a tool call anywhere was one line —
`log.info("latency: ... tools=%s", ...)` — emitted after the turn had
finished speaking, through a `basicConfig` with no file handler. It went to
stderr and left with the scrollback.

That is not a bookkeeping nicety. The incident this whole area came out of
was a post that went out and a turn that died before it could say so: the
system did a thing, and nothing anywhere could be asked what it had done.
A gate that stops an action is only half of it. The other half is a record
that survives the process.

Three rules this module keeps:

- It is written BEFORE the call resolves. A record made afterwards is
  missing exactly when it matters, which is when something died in the
  middle.
- It never raises. It runs inside the gate, and a store that will not write
  must not become an allow, nor a 500 the hook reads as a failure.
- It is bounded. It records every tool the brain reaches for, forever, on a
  machine nobody prunes.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import closing

from data_paths import db_path

log = logging.getLogger("jarvis.tool_log")

# Roughly a month of heavy use. Pruned in one statement on write, not by a
# sweeper nobody remembers to run.
MAX_ROWS = 20_000
_REASON_CAP = 500
# The gate's reason for a call it let through as a read, and nothing else
# (`server.internal_pretool`). `held_since` tells reads from actions by it.
READ_REASON = "Read-only."


def connect():
    conn = sqlite3.connect(str(db_path()), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(connect()) as conn, conn:
        conn.execute("""
          CREATE TABLE IF NOT EXISTS tool_calls(
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            at REAL NOT NULL,
            tool TEXT NOT NULL,
            server TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT NOT NULL,
            digest TEXT,
            action_id TEXT,
            tool_use_id TEXT,
            origin TEXT)""")
        conn.execute("CREATE INDEX IF NOT EXISTS tool_calls_at ON tool_calls(at)")


def record(*, tool: str, server: str, decision: str, reason: str = "",
           digest: str | None = None, action_id: str | None = None,
           tool_use_id: str | None = None, origin: str | None = None) -> None:
    """Write one decision. Never raises: the caller is the gate."""
    try:
        with closing(connect()) as conn, conn:
            conn.execute(
                "INSERT INTO tool_calls(at,tool,server,decision,reason,digest,"
                "action_id,tool_use_id,origin) VALUES(?,?,?,?,?,?,?,?,?)",
                (time.time(), str(tool), str(server), str(decision),
                 str(reason)[:_REASON_CAP], digest, action_id, tool_use_id, origin))
            # Every write, not every Nth. Amortised pruning is cheaper by an
            # indexed DELETE that usually matches nothing, and it buys a
            # ceiling of "MAX_ROWS plus however many since the last sweep" —
            # a bound with a fudge factor is one nobody can state. This one
            # is exactly MAX_ROWS, always.
            conn.execute(
                "DELETE FROM tool_calls WHERE seq <= "
                "(SELECT MAX(seq) FROM tool_calls) - ?", (MAX_ROWS,))
    except Exception:
        log.warning("tool log: could not record %s", tool, exc_info=True)


def recent(limit: int = 100, before: float | None = None) -> list[dict]:
    """The newest decisions first. Never raises."""
    try:
        with closing(connect()) as conn:
            if before is None:
                rows = conn.execute(
                    "SELECT * FROM tool_calls ORDER BY seq DESC LIMIT ?",
                    (int(limit),)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tool_calls WHERE at < ? ORDER BY seq DESC LIMIT ?",
                    (float(before), int(limit))).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        log.warning("tool log: could not read", exc_info=True)
        return []


def allowed_call(tool_use_id: str, tool: str) -> dict | None:
    """The gate's `allow` for this call — this `tool_use_id`, this tool — or
    None. What a post-call report is tied back to: only a call the gate
    actually let through has an outcome worth writing down, and its row
    names the card it was released on (`action_id`; none for a read).
    Never raises."""
    if not tool_use_id:
        return None
    try:
        with closing(connect()) as conn:
            row = conn.execute(
                "SELECT * FROM tool_calls WHERE tool_use_id=? AND tool=? AND decision='allow' "
                "ORDER BY seq DESC LIMIT 1", (str(tool_use_id), str(tool))).fetchone()
        return dict(row) if row else None
    except Exception:
        log.warning("tool log: could not look up %s", tool, exc_info=True)
        return None


def _names_since(since: float, decisions: tuple[str, ...]) -> set[str]:
    try:
        with closing(connect()) as conn:
            marks = ",".join("?" * len(decisions))
            rows = conn.execute(
                f"SELECT DISTINCT tool FROM tool_calls WHERE at >= ? "
                f"AND decision IN ({marks})", (float(since), *decisions)).fetchall()
        return {row["tool"] for row in rows}
    except Exception:
        log.warning("tool log: could not read", exc_info=True)
        return set()


def allowed_since(since: float) -> set[str]:
    """Tools whose calls the gate LET THROUGH after `since`.

    The question a killed turn actually has is not "was this tool ever
    allowed" but "was it allowed during this turn". Asking the unbounded
    question made a post submitted last week come back as something the
    turn had just done.
    """
    return _names_since(since, ("allow",))


def denied_since(since: float) -> set[str]:
    """Tools whose calls the gate REFUSED after `since`.

    Asked directly rather than derived as "attempted minus allowed": one
    turn can call the same tool twice, once allowed and once refused, and
    subtraction erases the refusal. Both facts are true and the user needs
    both — that pair IS the incident this came from.
    """
    return _names_since(since, ("deny",))


def held_since(since: float) -> set[str]:
    """Tools the gate did NOT let through as reads after `since`: staged,
    refused, or sent on an approval — whatever it took them to do.

    What the gate decided is the one account of a call that outlives the
    process that made it. A killed turn's process is gone by the time its
    account is asked, and with it the names its connectors reported, so the
    verdict cannot be derived again: `get&delete`, spelled `get_delete`,
    reads as a read without them."""
    try:
        with closing(connect()) as conn:
            rows = conn.execute(
                "SELECT DISTINCT tool FROM tool_calls WHERE at >= ? "
                "AND NOT (decision = 'allow' AND reason = ?)",
                (float(since), READ_REASON)).fetchall()
        return {row["tool"] for row in rows}
    except Exception:
        log.warning("tool log: could not read", exc_info=True)
        return set()


def attempted_since(since: float) -> set[str]:
    """Tools the brain REACHED FOR after `since`, however it was answered."""
    return _names_since(since, ("allow", "deny", "ask"))


def count() -> int:
    try:
        with closing(connect()) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0])
    except Exception:
        return 0
