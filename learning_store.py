"""What JARVIS has learned, and the evidence for it. SQLite, in jarvis.db.

Four kinds of row:

- an EXPERIENCE is one thing that happened — a run that finished, a tool
  that raised, a skill that failed, code the user had evaluated. It is the
  raw material and is never edited.
- a LESSON is a sentence of advice distilled from experiences (or told to
  JARVIS by the user), with a scope: `runs` lessons are handed to future
  Claude Code runs, `brain` lessons go into LESSONS.md for JARVIS himself.
  Each carries its own track record — how often it was applied and how often
  the work it was applied to succeeded.
- a SKILL is a small Python function JARVIS wrote and tested, versioned: an
  improvement is a new row, never an overwrite, so the version that worked
  yesterday is always still there.
- a SKILL CASE is an input a skill failed on — the regression suite that
  every later version of it must survive.

Lessons and skills move through one status enum: `candidate` (learned, not
yet trusted), `active` (in use), `retired` (kept as history, used by
nothing). Unlike memory (jarvis_memory.py) this is not prose the user edits
by hand: it is machine-kept evidence, so it lives with the other machine
records. The user's say over it is approve / retire, by voice or HTTP.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing

from data_paths import db_path

CANDIDATE, ACTIVE, RETIRED = "candidate", "active", "retired"
STATUSES = (CANDIDATE, ACTIVE, RETIRED)
SCOPES = ("runs", "brain")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path()), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with closing(_connect()) as conn, conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS learning_experiences (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         REAL NOT NULL,
                kind       TEXT NOT NULL,
                subject    TEXT NOT NULL DEFAULT '',
                outcome    TEXT NOT NULL,
                detail     TEXT NOT NULL DEFAULT '',
                ref        TEXT NOT NULL DEFAULT '',
                reflected  INTEGER NOT NULL DEFAULT 0,
                lesson_id  INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_learning_exp_pending
                ON learning_experiences(reflected, outcome);

            CREATE TABLE IF NOT EXISTS learning_lessons (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL,
                scope       TEXT NOT NULL,
                title       TEXT NOT NULL,
                body        TEXT NOT NULL,
                applies_to  TEXT NOT NULL DEFAULT '',
                source      TEXT NOT NULL,
                status      TEXT NOT NULL,
                uses        INTEGER NOT NULL DEFAULT 0,
                successes   INTEGER NOT NULL DEFAULT 0,
                failures    INTEGER NOT NULL DEFAULT 0,
                evidence    INTEGER NOT NULL DEFAULT 1,
                parents     TEXT NOT NULL DEFAULT '[]',
                note        TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS learning_applications (
                run_id     TEXT NOT NULL,
                lesson_id  INTEGER NOT NULL,
                settled    INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (run_id, lesson_id)
            );

            CREATE TABLE IF NOT EXISTS learning_skills (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL,
                version      INTEGER NOT NULL,
                created_at   REAL NOT NULL,
                description  TEXT NOT NULL,
                code         TEXT NOT NULL,
                tests        TEXT NOT NULL,
                status       TEXT NOT NULL,
                passed       INTEGER NOT NULL DEFAULT 0,
                total        INTEGER NOT NULL DEFAULT 0,
                calls        INTEGER NOT NULL DEFAULT 0,
                call_failures INTEGER NOT NULL DEFAULT 0,
                note         TEXT NOT NULL DEFAULT '',
                UNIQUE (name, version)
            );

            CREATE TABLE IF NOT EXISTS learning_skill_cases (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                name    TEXT NOT NULL,
                ts      REAL NOT NULL,
                kwargs  TEXT NOT NULL,
                error   TEXT NOT NULL
            );
        """)


# ---------------------------------------------------------------------------
# Experiences
# ---------------------------------------------------------------------------

def record_experience(kind: str, outcome: str, subject: str = "",
                      detail: str = "", ref: str = "", reflected: bool = False) -> int:
    """`reflected=True` records something that is not material for a lesson
    (code the user was trying out), so the reflection pass never reads it."""
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO learning_experiences (ts, kind, subject, outcome, detail, ref, "
            "reflected) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), kind, subject[:200], outcome, detail[:6000], ref[:200],
             int(reflected)))
        return int(cur.lastrowid)


def unreflected_failures(limit: int = 10) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM learning_experiences WHERE reflected = 0 AND outcome = 'failure' "
            "ORDER BY id LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def mark_reflected(experience_id: int, lesson_id: int | None = None) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE learning_experiences SET reflected = 1, lesson_id = ? "
                     "WHERE id = ?", (lesson_id, experience_id))


def recent_experiences(limit: int = 20) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT * FROM learning_experiences ORDER BY id DESC LIMIT ?",
                            (limit,)).fetchall()
    return [dict(r) for r in rows]


def experience_counts() -> dict:
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT outcome, COUNT(*) AS n FROM learning_experiences "
                            "GROUP BY outcome").fetchall()
        pending = conn.execute("SELECT COUNT(*) FROM learning_experiences WHERE "
                               "reflected = 0 AND outcome = 'failure'").fetchone()[0]
    counts = {r["outcome"]: r["n"] for r in rows}
    counts["unreflected_failures"] = pending
    return counts


# ---------------------------------------------------------------------------
# Lessons
# ---------------------------------------------------------------------------

def _lesson(row) -> dict:
    d = dict(row)
    d["parents"] = json.loads(d.get("parents") or "[]")
    return d


def add_lesson(scope: str, title: str, body: str, applies_to: str, source: str,
               status: str, parents: list[int] | None = None,
               uses: int = 0, successes: int = 0, failures: int = 0,
               evidence: int = 1) -> int:
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}")
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    now = time.time()
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO learning_lessons (created_at, updated_at, scope, title, body, "
            "applies_to, source, status, uses, successes, failures, evidence, parents) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, now, scope, title, body, applies_to, source, status, uses,
             successes, failures, evidence, json.dumps(parents or [])))
        return int(cur.lastrowid)


def get_lesson(lesson_id: int) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM learning_lessons WHERE id = ?",
                           (lesson_id,)).fetchone()
    return _lesson(row) if row else None


def list_lessons(status: str | list[str] | None = None, scope: str | None = None,
                 limit: int = 200) -> list[dict]:
    sql, params = "SELECT * FROM learning_lessons WHERE 1=1", []
    if status:
        statuses = [status] if isinstance(status, str) else list(status)
        sql += f" AND status IN ({','.join('?' * len(statuses))})"
        params += statuses
    if scope:
        sql += " AND scope = ?"
        params.append(scope)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with closing(_connect()) as conn:
        return [_lesson(r) for r in conn.execute(sql, params).fetchall()]


def set_lesson_status(lesson_id: int, status: str, note: str = "") -> bool:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    with closing(_connect()) as conn, conn:
        cur = conn.execute("UPDATE learning_lessons SET status = ?, note = ?, "
                           "updated_at = ? WHERE id = ?",
                           (status, note[:300], time.time(), lesson_id))
        return cur.rowcount > 0


def add_evidence(lesson_id: int) -> None:
    """Another experience led to the same lesson — it is seen more often,
    which says nothing yet about whether it helps."""
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE learning_lessons SET evidence = evidence + 1, "
                     "updated_at = ? WHERE id = ?", (time.time(), lesson_id))


def record_application(run_id: str, lesson_ids: list[int]) -> None:
    with closing(_connect()) as conn, conn:
        conn.executemany("INSERT OR IGNORE INTO learning_applications (run_id, lesson_id) "
                         "VALUES (?, ?)", [(run_id, i) for i in lesson_ids])
        conn.executemany("UPDATE learning_lessons SET uses = uses + 1 WHERE id = ?",
                         [(i,) for i in lesson_ids])


def baseline(lesson_ids: list[int]) -> tuple[int, int]:
    """(successes, failures) of the runs a lesson is compared against: every
    run with a verdict, in the projects the lesson was applied in, that did
    NOT carry it. `lesson_ids` is the lesson plus every lesson it was merged
    from, so a strategy is never compared against its own parents' runs.

    Dashboard runs never carry lessons, so they are the usual control. It is
    still observational — nobody chose at random which runs got the advice —
    but it measures a lesson against its own area's base rate rather than
    against nothing."""
    if not lesson_ids:
        return 0, 0
    marks = ",".join("?" * len(lesson_ids))
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"""SELECT outcome, COUNT(*) FROM learning_experiences
                WHERE kind = 'run' AND outcome IN ('success', 'failure')
                  AND subject IN (
                      SELECT e.subject FROM learning_experiences e
                      JOIN learning_applications a ON a.run_id = e.ref
                      WHERE e.kind = 'run' AND a.lesson_id IN ({marks}))
                  AND ref NOT IN (
                      SELECT run_id FROM learning_applications
                      WHERE lesson_id IN ({marks}))
                GROUP BY outcome""", [*lesson_ids, *lesson_ids]).fetchall()
    counts = {r[0]: r[1] for r in rows}
    return counts.get("success", 0), counts.get("failure", 0)


def merged_parent_ids() -> set[int]:
    """Every lesson that some strategy (in any status) was merged from."""
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT parents FROM learning_lessons "
                            "WHERE parents != '[]'").fetchall()
    return {int(i) for r in rows for i in json.loads(r[0] or "[]")}


def settle_run(run_id: str, success: bool | None) -> list[int]:
    """Credit (or blame) every lesson applied to this run, once.

    `None` means the outcome says nothing about the advice — a run the user
    cancelled — so the application is closed without a verdict."""
    column = None if success is None else ("successes" if success else "failures")
    with closing(_connect()) as conn, conn:
        ids = [r[0] for r in conn.execute(
            "SELECT lesson_id FROM learning_applications WHERE run_id = ? AND settled = 0",
            (run_id,)).fetchall()]
        conn.execute("UPDATE learning_applications SET settled = 1 WHERE run_id = ?",
                     (run_id,))
        if column:
            conn.executemany(f"UPDATE learning_lessons SET {column} = {column} + 1, "
                             "updated_at = ? WHERE id = ?",
                             [(time.time(), i) for i in ids])
    return ids


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

def _skill(row) -> dict:
    d = dict(row)
    d["tests"] = json.loads(d.get("tests") or "[]")
    return d


def add_skill(name: str, description: str, code: str, tests: list[dict],
              status: str, passed: int, total: int, note: str = "") -> dict:
    with closing(_connect()) as conn, conn:
        version = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM learning_skills "
                               "WHERE name = ?", (name,)).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO learning_skills (name, version, created_at, description, code, "
            "tests, status, passed, total, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, version, time.time(), description, code, json.dumps(tests),
             status, passed, total, note[:300]))
        row = conn.execute("SELECT * FROM learning_skills WHERE id = ?",
                           (cur.lastrowid,)).fetchone()
    return _skill(row)


def skill_versions(name: str) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT * FROM learning_skills WHERE name = ? "
                            "ORDER BY version DESC", (name,)).fetchall()
    return [_skill(r) for r in rows]


def active_skill(name: str) -> dict | None:
    for row in skill_versions(name):
        if row["status"] == ACTIVE:
            return row
    return None


def latest_skill(name: str) -> dict | None:
    versions = skill_versions(name)
    return versions[0] if versions else None


def list_skills() -> list[dict]:
    """The newest version of every skill, plus which version is active."""
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT * FROM learning_skills ORDER BY name, version DESC"
                            ).fetchall()
    seen: dict[str, dict] = {}
    for r in map(_skill, rows):
        entry = seen.setdefault(r["name"], {**r, "active_version": None})
        if r["status"] == ACTIVE and entry["active_version"] is None:
            entry["active_version"] = r["version"]
    return list(seen.values())


def activate_skill(name: str, version: int) -> bool:
    """Make one version the active one; the previously active one retires."""
    with closing(_connect()) as conn, conn:
        found = conn.execute("SELECT 1 FROM learning_skills WHERE name = ? AND version = ?",
                             (name, version)).fetchone()
        if not found:
            return False
        conn.execute("UPDATE learning_skills SET status = ? WHERE name = ? AND status = ?",
                     (RETIRED, name, ACTIVE))
        conn.execute("UPDATE learning_skills SET status = ? WHERE name = ? AND version = ?",
                     (ACTIVE, name, version))
        return True


def retire_skill(name: str) -> bool:
    with closing(_connect()) as conn, conn:
        cur = conn.execute("UPDATE learning_skills SET status = ? WHERE name = ? "
                           "AND status != ?", (RETIRED, name, RETIRED))
        return cur.rowcount > 0


def count_call(skill_id: int, ok: bool) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE learning_skills SET calls = calls + 1, "
                     "call_failures = call_failures + ? WHERE id = ?",
                     (0 if ok else 1, skill_id))


def add_skill_case(name: str, kwargs: dict, error: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("INSERT INTO learning_skill_cases (name, ts, kwargs, error) "
                     "VALUES (?, ?, ?, ?)",
                     (name, time.time(), json.dumps(kwargs), error[:500]))


def skill_cases(name: str, limit: int = 20) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT * FROM learning_skill_cases WHERE name = ? "
                            "ORDER BY id DESC LIMIT ?", (name, limit)).fetchall()
    return [{**dict(r), "kwargs": json.loads(r["kwargs"])} for r in rows]
