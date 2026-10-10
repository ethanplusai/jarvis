"""Durable conversation and idempotent client receipts, separate from runs."""
import sqlite3
import time
import uuid
from contextlib import closing

from data_paths import db_path
import schema


def connect():
    connection = sqlite3.connect(str(db_path()))
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with closing(connect()) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS conversation (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE, role TEXT NOT NULL, text TEXT NOT NULL,
            status TEXT NOT NULL, created_at REAL NOT NULL)""")
        # `deleted` arrived after installs already had the table. A message the
        # user removed from the panel keeps its row: this table is also how a
        # typed message is accepted exactly once (`accept` is INSERT OR IGNORE
        # on the client's id), so a hard delete would let a retried draft run
        # a second time. History filters on the stamp; a receipt still answers.
        schema.ensure_columns(conn, "conversation", {"deleted": "REAL"})


def accept(message_id: str, text: str) -> tuple[bool, dict]:
    """Claim once, across connections and restarts; never replay an old claim."""
    if not 1 <= len(message_id) <= 128 or not text.strip() or len(text) > 16000:
        raise ValueError("Message ID or text is invalid")
    with closing(connect()) as conn, conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO conversation(id,role,text,status,created_at) VALUES(?,?,?,?,?)",
            (message_id, "user", text, "accepted", time.time()))
        row = dict(conn.execute("SELECT * FROM conversation WHERE id=?", (message_id,)).fetchone())
        if row["text"] != text or row["role"] != "user":
            raise ValueError("Message ID already belongs to different content")
        return bool(cursor.rowcount), row


def record_assistant(text: str, message_id: str | None = None):
    if not text:
        return
    with closing(connect()) as conn, conn:
        conn.execute("INSERT OR IGNORE INTO conversation(id,role,text,status,created_at) VALUES(?,?,?,?,?)",
                     (message_id or str(uuid.uuid4()), "assistant", text, "delivered", time.time()))


def set_status(message_id, status):
    with closing(connect()) as conn, conn:
        conn.execute("UPDATE conversation SET status=? WHERE id=?", (status, message_id))


def get(message_id):
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM conversation WHERE id=?", (message_id,)).fetchone()
        return dict(row) if row else None


def list_messages(limit=100, before=None):
    with closing(connect()) as conn:
        rows = conn.execute("SELECT * FROM conversation WHERE seq < ? AND deleted IS NULL "
                            "ORDER BY seq DESC LIMIT ?",
                            (before or 9223372036854775807, max(1, min(limit, 500)))).fetchall()
        return [dict(r) for r in reversed(rows)]


def delete_message(message_id) -> bool:
    """Take one message out of history. Soft — see `init_db` for why. True
    when this call deleted it; False when it was already gone or never was."""
    with closing(connect()) as conn, conn:
        cursor = conn.execute("UPDATE conversation SET deleted=? WHERE id=? AND deleted IS NULL",
                              (time.time(), message_id))
        return cursor.rowcount == 1
