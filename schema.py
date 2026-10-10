"""The one way a column is added to a table that already exists.

`CREATE TABLE IF NOT EXISTS` never alters an existing table, so every column
that arrived after a release shipped needs a PRAGMA-then-ALTER on the live
database. Three modules had grown their own copy of that (run_store,
business_store, conversation_store) — three places to get it wrong, three
places a reviewer had to read. This is the one copy: the migration IS the
dict, the call is idempotent, and an identifier that is not one is refused
before it can be spliced into SQL. Data migrations that are more than a
column live under `migrations/`.
"""
import re
import sqlite3

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _ident(name: str, what: str) -> str:
    if not isinstance(name, str) or not _IDENT.fullmatch(name):
        raise ValueError(f"{what} is not an identifier: {name!r}")
    return name


def ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> list[str]:
    """Add each column of `columns` (`name -> DDL after the name`) that
    `table` does not have yet, in dict order. Returns the names added.
    Commits when it added anything; a no-op otherwise."""
    table = _ident(table, "table")
    for name in columns:
        _ident(name, "column")
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    added: list[str] = []
    for name, ddl in columns.items():
        if name in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        added.append(name)
    if added:
        conn.commit()
    return added
