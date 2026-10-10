"""One way to add a column to a table that already exists.

`CREATE TABLE IF NOT EXISTS` never alters an existing table, so every column
added after a release shipped needs a PRAGMA-then-ALTER — and three modules
had grown their own copy (run_store, business_store, conversation_store).
`schema.ensure_columns` is the one copy: the migration IS the dict, it is
idempotent, and it refuses an identifier it would otherwise splice into SQL.
"""

import sqlite3
from contextlib import closing

import pytest

import schema


def _fresh():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    return conn


def _columns(conn):
    return [row[1] for row in conn.execute("PRAGMA table_info(t)")]


def test_missing_columns_are_added_in_order_and_reported():
    with closing(_fresh()) as conn:
        added = schema.ensure_columns(conn, "t", {"deleted": "REAL", "note": "TEXT DEFAULT ''"})
        assert added == ["deleted", "note"]
        assert _columns(conn) == ["id", "name", "deleted", "note"]
        assert conn.execute("SELECT note FROM t").fetchall() == []


def test_present_columns_are_left_alone_and_the_call_is_idempotent():
    with closing(_fresh()) as conn:
        schema.ensure_columns(conn, "t", {"deleted": "REAL"})
        assert schema.ensure_columns(conn, "t", {"name": "TEXT", "deleted": "REAL"}) == []
        assert _columns(conn) == ["id", "name", "deleted"]


def test_existing_rows_get_the_default():
    with closing(_fresh()) as conn:
        conn.execute("INSERT INTO t(name) VALUES ('a')")
        schema.ensure_columns(conn, "t", {"flag": "INTEGER NOT NULL DEFAULT 0"})
        assert conn.execute("SELECT flag FROM t").fetchone()[0] == 0


@pytest.mark.parametrize("table,column", [
    ("t; DROP TABLE t", "x"), ("t", "x; DROP TABLE t"), ("", "x"), ("t", "bad-name"), ("1t", "x"),
])
def test_an_identifier_that_is_not_one_is_refused_before_it_reaches_sql(table, column):
    with closing(_fresh()) as conn:
        with pytest.raises(ValueError):
            schema.ensure_columns(conn, table, {column: "TEXT"})
        assert _columns(conn) == ["id", "name"]


def test_the_three_stores_use_it_rather_than_their_own_copy():
    """The point of the helper is that there is one. A store that still
    spells out PRAGMA-then-ALTER has drifted back to three copies."""
    from pathlib import Path
    root = Path(__file__).parent.parent
    for name in ("run_store.py", "business_store.py", "conversation_store.py"):
        src = (root / name).read_text(encoding="utf-8")
        assert "ensure_columns(" in src, name
        assert "ALTER TABLE" not in src, f"{name} still alters tables by hand"
