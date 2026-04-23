"""
JARVIS Memory & Planning — persistent context, tasks, notes, and smart routing.

Three systems:
1. Memory — facts, preferences, project context JARVIS learns from conversations
2. Tasks — to-do items with priority, due dates, project association
3. Notes — freeform context tied to projects, people, or topics

Everything stored in SQLite. Relevant memories injected into every LLM call
so JARVIS gets smarter over time.
"""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

FAST_MODEL = os.getenv("JARVIS_FAST_MODEL", "claude-haiku-4-5-20251001")

log = logging.getLogger("jarvis.memory")

DB_PATH = Path(__file__).parent / "data" / "jarvis.db"


def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,          -- 'fact', 'preference', 'project', 'person', 'decision'
            content TEXT NOT NULL,
            source TEXT DEFAULT '',      -- what conversation/context it came from
            importance INTEGER DEFAULT 5, -- 1-10, higher = more important
            created_at REAL NOT NULL,
            last_accessed REAL,
            access_count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            priority TEXT DEFAULT 'medium', -- 'high', 'medium', 'low'
            status TEXT DEFAULT 'open',     -- 'open', 'in_progress', 'done', 'cancelled'
            due_date TEXT,                  -- ISO date string
            due_time TEXT,                  -- HH:MM
            project TEXT DEFAULT '',
            tags TEXT DEFAULT '[]',         -- JSON array
            notes TEXT DEFAULT '',
            created_at REAL NOT NULL,
            completed_at REAL
        );

        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT DEFAULT '',
            content TEXT NOT NULL,
            topic TEXT DEFAULT '',       -- project name, person, or topic
            tags TEXT DEFAULT '[]',      -- JSON array
            created_at REAL NOT NULL,
            updated_at REAL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
            content, type, source,
            content='memories', content_rowid='id'
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS task_fts USING fts5(
            title, description, project, notes,
            content='tasks', content_rowid='id'
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS note_fts USING fts5(
            title, content, topic,
            content='notes', content_rowid='id'
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp REAL NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS conversation_fts USING fts5(
            content, session_id,
            content='conversations', content_rowid='id'
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,
            started_at REAL NOT NULL,
            ended_at REAL,
            summary TEXT DEFAULT '',
            message_count INTEGER DEFAULT 0
        );
    """)
    conn.close()
    log.info("Memory database initialized")


# ---------------------------------------------------------------------------
# Memories — facts JARVIS learns
# ---------------------------------------------------------------------------

def remember(content: str, mem_type: str = "fact", source: str = "", importance: int = 5) -> int:
    """Store a memory. Returns the memory ID."""
    conn = _get_db()
    cur = conn.execute(
        "INSERT INTO memories (type, content, source, importance, created_at) VALUES (?, ?, ?, ?, ?)",
        (mem_type, content, source, importance, time.time())
    )
    mem_id = cur.lastrowid
    # Update FTS
    conn.execute(
        "INSERT INTO memory_fts (rowid, content, type, source) VALUES (?, ?, ?, ?)",
        (mem_id, content, mem_type, source)
    )
    conn.commit()
    conn.close()
    log.info(f"Stored memory [{mem_type}]: {content[:60]}")
    return mem_id


def _sanitize_fts_query(query: str) -> str:
    """Clean a query string for FTS5 — remove special characters that break it."""
    # Remove apostrophes, quotes, and FTS operators
    cleaned = query.replace("'", "").replace('"', "").replace("*", "").replace("-", " ")
    # Take meaningful words only
    words = [w for w in cleaned.split() if len(w) > 2]
    if not words:
        return ""
    # Join with OR for broader matching
    return " OR ".join(words[:5])


def recall(query: str, limit: int = 5) -> list[dict]:
    """Search memories by relevance. Returns most relevant matches."""
    fts_query = _sanitize_fts_query(query)
    if not fts_query:
        return []
    conn = _get_db()
    try:
        results = conn.execute("""
            SELECT m.id, m.type, m.content, m.importance, m.created_at, m.access_count
            FROM memory_fts f
            JOIN memories m ON f.rowid = m.id
            WHERE memory_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (fts_query, limit)).fetchall()
    except Exception:
        results = []

    # Update access counts
    for r in results:
        conn.execute(
            "UPDATE memories SET last_accessed = ?, access_count = access_count + 1 WHERE id = ?",
            (time.time(), r["id"])
        )
    conn.commit()
    conn.close()
    return [dict(r) for r in results]


def get_recent_memories(limit: int = 10) -> list[dict]:
    """Get most recent memories."""
    conn = _get_db()
    results = conn.execute(
        "SELECT * FROM memories ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in results]


def get_important_memories(limit: int = 10) -> list[dict]:
    """Get highest importance memories."""
    conn = _get_db()
    results = conn.execute(
        "SELECT * FROM memories ORDER BY importance DESC, access_count DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in results]


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

def create_task(title: str, description: str = "", priority: str = "medium",
                due_date: str = "", due_time: str = "", project: str = "",
                tags: list[str] = None) -> int:
    """Create a task. Returns task ID."""
    conn = _get_db()
    cur = conn.execute(
        """INSERT INTO tasks (title, description, priority, due_date, due_time,
           project, tags, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (title, description, priority, due_date, due_time,
         project, json.dumps(tags or []), time.time())
    )
    task_id = cur.lastrowid
    conn.execute(
        "INSERT INTO task_fts (rowid, title, description, project, notes) VALUES (?, ?, ?, ?, ?)",
        (task_id, title, description, project, "")
    )
    conn.commit()
    conn.close()
    log.info(f"Created task [{priority}]: {title}")
    return task_id


def get_open_tasks(project: str = None) -> list[dict]:
    """Get all open/in-progress tasks, optionally filtered by project."""
    conn = _get_db()
    if project:
        results = conn.execute(
            "SELECT * FROM tasks WHERE status IN ('open','in_progress') AND project LIKE ? ORDER BY "
            "CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, due_date",
            (f"%{project}%",)
        ).fetchall()
    else:
        results = conn.execute(
            "SELECT * FROM tasks WHERE status IN ('open','in_progress') ORDER BY "
            "CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, due_date"
        ).fetchall()
    conn.close()
    return [dict(r) for r in results]


def get_tasks_for_date(date_str: str) -> list[dict]:
    """Get tasks due on a specific date (YYYY-MM-DD)."""
    conn = _get_db()
    results = conn.execute(
        "SELECT * FROM tasks WHERE due_date = ? AND status != 'cancelled' ORDER BY "
        "CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, due_time",
        (date_str,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in results]


def complete_task(task_id: int):
    """Mark a task as done."""
    conn = _get_db()
    conn.execute(
        "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
        (time.time(), task_id)
    )
    conn.commit()
    conn.close()


def search_tasks(query: str, limit: int = 10) -> list[dict]:
    """Search tasks by text."""
    fts_query = _sanitize_fts_query(query)
    if not fts_query:
        return []
    conn = _get_db()
    try:
        results = conn.execute("""
            SELECT t.* FROM task_fts f
            JOIN tasks t ON f.rowid = t.id
            WHERE task_fts MATCH ?
            ORDER BY rank LIMIT ?
        """, (fts_query, limit)).fetchall()
    except Exception:
        results = []
    conn.close()
    return [dict(r) for r in results]


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

def create_note(content: str, title: str = "", topic: str = "", tags: list[str] = None) -> int:
    """Create a note. Returns note ID."""
    conn = _get_db()
    now = time.time()
    cur = conn.execute(
        "INSERT INTO notes (title, content, topic, tags, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (title, content, topic, json.dumps(tags or []), now, now)
    )
    note_id = cur.lastrowid
    conn.execute(
        "INSERT INTO note_fts (rowid, title, content, topic) VALUES (?, ?, ?, ?)",
        (note_id, title, content, topic)
    )
    conn.commit()
    conn.close()
    log.info(f"Created note: {title or content[:40]}")
    return note_id


def search_notes(query: str, limit: int = 10) -> list[dict]:
    """Search notes by text."""
    fts_query = _sanitize_fts_query(query)
    if not fts_query:
        return []
    conn = _get_db()
    try:
        results = conn.execute("""
            SELECT n.* FROM note_fts f
            JOIN notes n ON f.rowid = n.id
            WHERE note_fts MATCH ?
            ORDER BY rank LIMIT ?
        """, (fts_query, limit)).fetchall()
    except Exception:
        results = []
    conn.close()
    return [dict(r) for r in results]


def get_notes_by_topic(topic: str) -> list[dict]:
    """Get all notes for a topic/project."""
    conn = _get_db()
    results = conn.execute(
        "SELECT * FROM notes WHERE topic LIKE ? ORDER BY updated_at DESC",
        (f"%{topic}%",)
    ).fetchall()
    conn.close()
    return [dict(r) for r in results]


# ---------------------------------------------------------------------------
# Context Builder — smart context for LLM calls
# ---------------------------------------------------------------------------

def build_memory_context(user_message: str) -> str:
    """Build relevant context from memories, tasks, and conversation history for the LLM.

    Searches for relevant memories and past exchanges based on what the user is saying.
    Fast — runs FTS queries, no heavy computation.
    """
    parts = []
    relevant = []

    # Always include: open high-priority tasks
    high_tasks = [t for t in get_open_tasks() if t["priority"] == "high"]
    if high_tasks:
        task_lines = [f"  - [{t['priority']}] {t['title']}" +
                      (f" (due {t['due_date']})" if t["due_date"] else "")
                      for t in high_tasks[:5]]
        parts.append("HIGH PRIORITY TASKS:\n" + "\n".join(task_lines))

    # Search memories relevant to what user is saying
    if len(user_message) > 5:
        relevant = recall(user_message, limit=4)
        if relevant:
            mem_lines = [f"  - [{m['type']}] {m['content']}" for m in relevant]
            parts.append("RELEVANT MEMORIES:\n" + "\n".join(mem_lines))

    # Recent important memories (always available)
    important = get_important_memories(limit=4)
    if important:
        seen = {m["content"] for m in relevant}
        imp_lines = [f"  - {m['content']}" for m in important if m["content"] not in seen]
        if imp_lines:
            parts.append("KEY FACTS:\n" + "\n".join(imp_lines[:4]))

    # Search past conversations for relevant exchanges
    if len(user_message) > 10:
        past = search_conversations(user_message, limit=3)
        if past:
            seen_content = {m["content"] for m in relevant}
            past_lines = []
            for c in past:
                snippet = c["content"][:120].replace("\n", " ")
                if snippet not in seen_content:
                    dt = datetime.fromtimestamp(c["timestamp"]).strftime("%d %b")
                    who = "You" if c["role"] == "user" else "JARVIS"
                    past_lines.append(f"  - [{dt}] {who}: {snippet}")
            if past_lines:
                parts.append("RELEVANT PAST EXCHANGES:\n" + "\n".join(past_lines))

    return "\n\n".join(parts) if parts else ""


def format_tasks_for_voice(tasks: list[dict]) -> str:
    """Format tasks for voice response."""
    if not tasks:
        return "No tasks on the list, sir."
    count = len(tasks)
    high = [t for t in tasks if t["priority"] == "high"]
    if count == 1:
        t = tasks[0]
        return f"One task: {t['title']}." + (f" Due {t['due_date']}." if t["due_date"] else "")
    result = f"You have {count} open tasks."
    if high:
        result += f" {len(high)} are high priority."
    top = tasks[:3]
    for t in top:
        result += f" {t['title']}."
    if count > 3:
        result += f" And {count - 3} more."
    return result


def format_plan_for_voice(tasks: list[dict], events: list[dict]) -> str:
    """Format a day plan combining tasks and calendar events."""
    if not tasks and not events:
        return "Your day looks clear, sir. No events or tasks scheduled."

    parts = []
    if events:
        parts.append(f"{len(events)} events on the calendar")
    if tasks:
        high = [t for t in tasks if t["priority"] == "high"]
        parts.append(f"{len(tasks)} tasks" + (f", {len(high)} high priority" if high else ""))

    result = f"For tomorrow: {', '.join(parts)}. "

    # List events first
    if events:
        for e in events[:3]:
            result += f"{e.get('start', '')} {e['title']}. "

    # Then high priority tasks
    if tasks:
        for t in [t for t in tasks if t["priority"] == "high"][:2]:
            result += f"Priority: {t['title']}. "

    result += "Shall I adjust anything?"
    return result


# ---------------------------------------------------------------------------
# Memory extraction — learn from conversations
# ---------------------------------------------------------------------------

async def extract_memories(user_text: str, jarvis_response: str, anthropic_client) -> list[str]:
    """After a conversation turn, extract any facts worth remembering.

    Uses Haiku to decide if anything in the exchange is worth storing.
    Returns list of memories stored.
    """
    if not anthropic_client or len(user_text) < 15:
        return []

    try:
        response = await anthropic_client.messages.create(
            model=FAST_MODEL,
            max_tokens=400,
            system=(
                "You are a memory extraction engine for JARVIS, an AI assistant. "
                "Extract every fact worth remembering from this conversation exchange. "
                "Be thorough — capture:\n"
                "- Personal facts: name, location, job, family, finances, health\n"
                "- Preferences: likes, dislikes, habits, routines, communication style\n"
                "- Projects: what's being built, tech stack, status, goals\n"
                "- Decisions: choices made, approaches agreed on\n"
                "- Goals & plans: short and long-term intentions\n"
                "- People mentioned: names, roles, relationships\n"
                "- Recurring topics: anything the user cares deeply about\n"
                "Do NOT extract greetings, filler, or things already universally known. "
                "Return a JSON array: [{\"type\": \"fact|preference|project|person|decision|goal\", "
                "\"content\": \"concise statement of the fact\", \"importance\": 1-10}]. "
                "Return [] only if truly nothing notable was said. Importance 8-10 for personal/financial/health facts."
            ),
            messages=[{"role": "user", "content": f"User: {user_text}\nJARVIS: {jarvis_response}"}],
        )

        text = response.content[0].text.strip()
        # Parse JSON
        if text.startswith("["):
            items = json.loads(text)
            stored = []
            for item in items:
                if isinstance(item, dict) and "content" in item:
                    remember(
                        content=item["content"],
                        mem_type=item.get("type", "fact"),
                        source=user_text[:50],
                        importance=item.get("importance", 5),
                    )
                    stored.append(item["content"])
            return stored
    except Exception as e:
        log.debug(f"Memory extraction failed: {e}")

    return []


# ---------------------------------------------------------------------------
# Conversation log — every exchange persisted across sessions
# ---------------------------------------------------------------------------

def log_message(session_id: str, role: str, content: str) -> None:
    """Append a single message to the persistent conversation log."""
    conn = _get_db()
    cur = conn.execute(
        "INSERT INTO conversations (session_id, role, content, timestamp) VALUES (?,?,?,?)",
        (session_id, role, content, time.time())
    )
    conn.execute(
        "INSERT INTO conversation_fts (rowid, content, session_id) VALUES (?,?,?)",
        (cur.lastrowid, content, session_id)
    )
    conn.commit()
    conn.close()


def search_conversations(query: str, limit: int = 5) -> list[dict]:
    """Full-text search across all logged conversation messages."""
    fts_query = _sanitize_fts_query(query)
    if not fts_query:
        return []
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT c.session_id, c.role, c.content, c.timestamp "
            "FROM conversation_fts f "
            "JOIN conversations c ON c.id = f.rowid "
            "WHERE conversation_fts MATCH ? "
            "ORDER BY c.timestamp DESC LIMIT ?",
            (fts_query, limit)
        ).fetchall()
    except Exception:
        rows = []
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Session tracking — summaries persisted across server restarts
# ---------------------------------------------------------------------------

def start_session(session_id: str) -> None:
    """Record the start of a new JARVIS session."""
    conn = _get_db()
    conn.execute(
        "INSERT OR IGNORE INTO sessions (session_id, started_at) VALUES (?,?)",
        (session_id, time.time())
    )
    conn.commit()
    conn.close()
    log.info(f"Session started: {session_id}")


def end_session(session_id: str, summary: str = "", message_count: int = 0) -> None:
    """Record session end with a Haiku-generated summary."""
    conn = _get_db()
    conn.execute(
        "UPDATE sessions SET ended_at=?, summary=?, message_count=? WHERE session_id=?",
        (time.time(), summary, message_count, session_id)
    )
    conn.commit()
    conn.close()
    log.info(f"Session saved: {session_id} ({message_count} exchanges)")


def get_recent_sessions(limit: int = 5) -> list[dict]:
    """Return the most recent sessions that have a summary (most recent first)."""
    conn = _get_db()
    rows = conn.execute(
        "SELECT session_id, started_at, ended_at, summary, message_count "
        "FROM sessions WHERE summary != '' AND summary IS NOT NULL "
        "ORDER BY started_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def build_session_context() -> str:
    """Build a cross-session memory block from recent past session summaries.

    Called once per WebSocket connection so JARVIS remembers previous conversations.
    """
    sessions = get_recent_sessions(limit=5)
    if not sessions:
        return ""

    lines = []
    for s in sessions:
        if not s.get("summary"):
            continue
        dt = datetime.fromtimestamp(s["started_at"]).strftime("%a %d %b")
        count = s.get("message_count") or 0
        suffix = f" ({count} exchanges)" if count else ""
        lines.append(f"[{dt}{suffix}] {s['summary']}")

    if not lines:
        return ""

    return "PREVIOUS SESSIONS (most recent first):\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

def prune_old_conversations(days: int = 60) -> int:
    """Delete conversation log entries older than `days` days. Returns rows deleted."""
    cutoff = time.time() - days * 86400
    conn = _get_db()
    # Remove FTS entries first
    conn.execute(
        "DELETE FROM conversation_fts WHERE rowid IN "
        "(SELECT id FROM conversations WHERE timestamp < ?)", (cutoff,)
    )
    cur = conn.execute("DELETE FROM conversations WHERE timestamp < ?", (cutoff,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    if deleted:
        log.info(f"Pruned {deleted} conversation entries older than {days} days")
    return deleted


# Initialize on import
init_db()
