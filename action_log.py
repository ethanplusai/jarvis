"""Action log and confirmation queue for JARVIS.

This module records the things JARVIS does outside the conversation and holds
potentially irreversible actions until the user explicitly confirms them. It is
intentionally small and dependency-free so every execution path can log through
it before MCP write-access or executable skills expand further.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).parent / "data" / "jarvis.db"

WRITE_KEYWORDS = (
    "send", "post", "create", "update", "delete", "charge", "refund", "pay",
    "invoice", "draft", "schedule", "label", "move", "archive", "write", "log",
)
OUTBOUND_SERVERS = {"gmail", "slack", "hubspot", "stripe", "notion", "google-calendar", "github"}


def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_action_log_db() -> None:
    conn = _get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS action_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at REAL NOT NULL,
            action_type TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            risk TEXT DEFAULT 'low',
            target TEXT DEFAULT '',
            details TEXT DEFAULT '{}',
            result TEXT DEFAULT '{}',
            confirmed_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_action_log_created ON action_log(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_action_log_status ON action_log(status);
        """
    )
    conn.commit()
    conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("details", "result"):
        try:
            data[key] = json.loads(data.get(key) or "{}")
        except json.JSONDecodeError:
            data[key] = {}
    return data


def risk_for_tool(server_id: str, tool_name: str, arguments: dict[str, Any] | None = None) -> str:
    """Classify MCP tool-call risk from server, tool name, and argument hints."""
    text = f"{server_id} {tool_name} {json.dumps(arguments or {}, sort_keys=True)}".lower()
    if server_id in OUTBOUND_SERVERS and any(word in text for word in WRITE_KEYWORDS):
        return "high"
    if any(word in text for word in WRITE_KEYWORDS):
        return "medium"
    return "low"


def requires_confirmation(risk: str) -> bool:
    return risk in {"medium", "high"}


def record_action(
    action_type: str,
    title: str,
    *,
    status: str = "completed",
    risk: str = "low",
    target: str = "",
    details: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    conn = _get_db()
    cur = conn.execute(
        "INSERT INTO action_log (created_at, action_type, title, status, risk, target, details, result, confirmed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            time.time(),
            action_type,
            title,
            status,
            risk,
            target,
            json.dumps(details or {}),
            json.dumps(result or {}),
            time.time() if status == "completed" and risk != "low" else None,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM action_log WHERE id = ?", (cur.lastrowid,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def create_pending(action_type: str, title: str, *, risk: str, target: str, details: dict[str, Any]) -> dict[str, Any]:
    return record_action(action_type, title, status="pending_confirmation", risk=risk, target=target, details=details)


def list_actions(limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
    conn = _get_db()
    if status:
        rows = conn.execute(
            "SELECT * FROM action_log WHERE status = ? ORDER BY created_at DESC LIMIT ?", (status, limit)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM action_log ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_action(action_id: int) -> dict[str, Any] | None:
    conn = _get_db()
    row = conn.execute("SELECT * FROM action_log WHERE id = ?", (action_id,)).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def mark_completed(action_id: int, result: dict[str, Any]) -> dict[str, Any] | None:
    conn = _get_db()
    conn.execute(
        "UPDATE action_log SET status='completed', result=?, confirmed_at=? WHERE id=?",
        (json.dumps(result), time.time(), action_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM action_log WHERE id = ?", (action_id,)).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def mark_cancelled(action_id: int) -> dict[str, Any] | None:
    conn = _get_db()
    conn.execute("UPDATE action_log SET status='cancelled' WHERE id=?", (action_id,))
    conn.commit()
    row = conn.execute("SELECT * FROM action_log WHERE id = ?", (action_id,)).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


init_action_log_db()
