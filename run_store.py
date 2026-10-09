"""The single source of truth for Claude Code runs.

Every execution path writes here. The dashboard only reads. State
transitions are persisted before they are announced.
"""

import logging
import sqlite3
import time
import uuid
from contextlib import closing

from data_paths import db_path

log = logging.getLogger("jarvis.run_store")


class RunStatus:
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"

    ALL = (QUEUED, RUNNING, SUCCEEDED, FAILED, TIMED_OUT, CANCELLED)
    TERMINAL = frozenset({SUCCEEDED, FAILED, TIMED_OUT, CANCELLED})
    ACTIVE = frozenset({QUEUED, RUNNING})


_UPDATABLE = frozenset({
    "status", "result_text", "summary", "error", "exit_code", "pid",
    "cost_usd", "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_creation_tokens", "num_turns", "model", "requested_model",
    "started_at", "ended_at", "project_path", "project_name", "is_error",
})


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with closing(_connect()) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id            TEXT PRIMARY KEY,
                project_name  TEXT NOT NULL,
                project_path  TEXT NOT NULL,
                prompt        TEXT NOT NULL,
                origin        TEXT NOT NULL,
                status        TEXT NOT NULL,
                resume_from   TEXT,
                result_text   TEXT DEFAULT '',
                summary       TEXT DEFAULT '',
                error         TEXT DEFAULT '',
                exit_code     INTEGER,
                pid           INTEGER,
                cost_usd      REAL    DEFAULT 0,
                input_tokens  INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_read_tokens     INTEGER DEFAULT 0,
                cache_creation_tokens INTEGER DEFAULT 0,
                num_turns     INTEGER DEFAULT 0,
                model         TEXT DEFAULT '',
                requested_model TEXT DEFAULT '',
                is_error      INTEGER DEFAULT 0,
                created_at    REAL NOT NULL,
                started_at    REAL,
                ended_at      REAL
            );
            CREATE INDEX IF NOT EXISTS idx_runs_status  ON runs(status);
            CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);

            CREATE TABLE IF NOT EXISTS run_events (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id  TEXT    NOT NULL REFERENCES runs(id),
                seq     INTEGER NOT NULL,
                ts      REAL    NOT NULL,
                kind    TEXT    NOT NULL,
                payload TEXT    NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_events_run_seq
                ON run_events(run_id, seq);

            CREATE TABLE IF NOT EXISTS steers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                voice_name TEXT NOT NULL,
                project TEXT NOT NULL,
                prompt TEXT NOT NULL,
                outcome TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_steers_created ON steers(created_at DESC);
        """)
        conn.commit()

        # `CREATE TABLE IF NOT EXISTS` never alters a table that already
        # exists, so a live jarvis.db predating a column needs an explicit
        # backfill. Same PRAGMA-table_info-then-ALTER pattern as
        # migrations/001_dispatches_to_runs.py.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
        if "requested_model" not in cols:
            conn.execute(
                "ALTER TABLE runs ADD COLUMN requested_model TEXT DEFAULT ''")
            conn.commit()
        if "is_error" not in cols:
            conn.execute(
                "ALTER TABLE runs ADD COLUMN is_error INTEGER DEFAULT 0")
            conn.commit()


def create_run(prompt: str, project_name: str, project_path: str,
               origin: str, resume_from: str | None = None) -> str:
    run_id = str(uuid.uuid4())
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO runs (id, project_name, project_path, prompt, origin, "
            "status, resume_from, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, project_name, project_path, prompt, origin,
             RunStatus.QUEUED, resume_from, time.time()),
        )
        conn.commit()
    log.info("run %s created (%s, origin=%s)", run_id, project_name, origin)
    return run_id


def get_run(run_id: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None


def all_run_ids() -> set[str]:
    """Every run id ever recorded.

    A run id is ALSO the Claude Code session id: `run_executor._command`
    passes it as `--session-id`. That makes this the exact set of roster
    sessions JARVIS started himself, which is what lets the voice path tell
    its own one-shot runs apart from the user's real conversations.
    """
    with closing(_connect()) as conn:
        return {r["id"] for r in conn.execute("SELECT id FROM runs")}


def list_runs(status: list[str] | None = None, project: str | None = None,
              limit: int = 50, before: float | None = None) -> list[dict]:
    sql = "SELECT * FROM runs WHERE 1=1"
    params: list = []
    if status:
        sql += f" AND status IN ({','.join('?' * len(status))})"
        params.extend(status)
    if project:
        sql += " AND project_name LIKE ?"
        params.append(f"%{project}%")
    if before is not None:
        sql += " AND created_at < ?"
        params.append(before)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    with closing(_connect()) as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def update_run(run_id: str, **fields) -> None:
    if not fields:
        return
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(f"not updatable: {sorted(unknown)}")
    assignments = ", ".join(f"{k}=?" for k in fields)
    with closing(_connect()) as conn:
        conn.execute(f"UPDATE runs SET {assignments} WHERE id=?",
                     (*fields.values(), run_id))
        conn.commit()


def next_seq(run_id: str) -> int:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM run_events WHERE run_id=?",
            (run_id,)).fetchone()
        return int(row["m"]) + 1


def append_event(run_id: str, seq: int, kind: str, payload: str) -> None:
    append_events(run_id, [(seq, kind, payload)])


def append_events(run_id: str, rows: list[tuple[int, str, str]]) -> None:
    """Insert many events in one transaction.

    The executor batches through this so a chatty build costs one commit per
    batch rather than one per event.
    """
    if not rows:
        return
    now = time.time()
    with closing(_connect()) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO run_events (run_id, seq, ts, kind, payload) "
            "VALUES (?,?,?,?,?)",
            [(run_id, seq, now, kind, payload) for seq, kind, payload in rows])
        conn.commit()


def get_events(run_id: str, after_seq: int = 0, limit: int = 200) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM run_events WHERE run_id=? AND seq>? "
            "ORDER BY seq ASC LIMIT ?",
            (run_id, after_seq, limit)).fetchall()
        return [dict(r) for r in rows]


def count_events(run_id: str) -> int:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM run_events WHERE run_id=?",
            (run_id,)).fetchone()
        return int(row["n"])


def sweep_stale_runs() -> int:
    """Mark runs left active by a crashed server as failed."""
    with closing(_connect()) as conn:
        cur = conn.execute(
            "UPDATE runs SET status=?, error=?, ended_at=? WHERE status IN (?,?)",
            (RunStatus.FAILED, "server restarted during run", time.time(),
             RunStatus.QUEUED, RunStatus.RUNNING))
        conn.commit()
        count = cur.rowcount
    if count:
        log.warning("swept %d stale run(s) after restart", count)
    return count


_PERIODS = {"day": 86400, "week": 86400 * 7, "month": 86400 * 30}


def stats(period: str = "day") -> dict:
    cutoff = time.time() - _PERIODS.get(period, 86400)
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n, "
            "COALESCE(SUM(cost_usd),0) AS cost, "
            "COALESCE(SUM(input_tokens),0) AS inp, "
            "COALESCE(SUM(output_tokens),0) AS out "
            "FROM runs WHERE created_at >= ? GROUP BY status",
            (cutoff,)).fetchall()

    by_status = {s: 0 for s in RunStatus.ALL}
    total_cost = total_in = total_out = 0
    for r in rows:
        by_status[r["status"]] = r["n"]
        total_cost += r["cost"]
        total_in += r["inp"]
        total_out += r["out"]

    return {
        "period": period,
        "by_status": by_status,
        "total_runs": sum(by_status.values()),
        "total_cost_usd": round(total_cost, 6),
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
    }


def record_steer(session_id: str, voice_name: str, project: str,
                 prompt: str, outcome: str) -> None:
    """Every steer is recorded, including the refused ones — 'did you send
    that?' must have an answer."""
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO steers (session_id, voice_name, project, prompt, outcome, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, voice_name, project, prompt, outcome, time.time()))
        conn.commit()


def list_steers(limit: int = 50) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            # id breaks ties: on Windows time.time() ticks coarsely enough
            # that two steers recorded back to back share a created_at.
            "SELECT * FROM steers ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
