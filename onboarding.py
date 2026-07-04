"""
JARVIS Onboarding — first-run discovery and user profiling.

On a fresh install JARVIS doesn't know who it's working for. This module tracks
a lightweight onboarding state machine and a persistent user profile so that on
the first conversation JARVIS runs a short discovery dialogue — who the user is,
what kind of business/role, and what they want to accomplish — then uses that to
recommend skills and tool connections (MCP) and build a starting workflow.

The conversation itself is driven by the LLM via instructions injected into the
system prompt; this module supplies the state, stores answers, and decides when
onboarding is complete.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("jarvis.onboarding")

DB_PATH = Path(__file__).parent / "data" / "jarvis.db"

# Discovery topics JARVIS should learn during onboarding, in order.
DISCOVERY_FIELDS = [
    ("name", "what the user would like to be called"),
    ("role", "the user's role or job title"),
    ("business", "the type of business or industry they work in"),
    ("team_size", "roughly how big their team or company is"),
    ("goals", "the main tasks or goals they want JARVIS to help with"),
    ("tools", "the tools and apps they already use (email, calendar, CRM, docs, etc.)"),
]


def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_onboarding_db():
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS user_profile (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS onboarding_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            status TEXT DEFAULT 'pending',   -- pending, in_progress, completed, skipped
            turns INTEGER DEFAULT 0,
            started_at REAL,
            completed_at REAL
        );
        INSERT OR IGNORE INTO onboarding_state (id, status) VALUES (1, 'pending');
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

def set_profile(key: str, value: str):
    conn = _get_db()
    conn.execute(
        "INSERT INTO user_profile (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, time.time()),
    )
    conn.commit()
    conn.close()


def set_profile_many(data: dict):
    for k, v in data.items():
        if v is not None and str(v).strip():
            set_profile(k, str(v).strip())


def get_profile() -> dict:
    conn = _get_db()
    rows = conn.execute("SELECT key, value FROM user_profile").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def get_state() -> dict:
    conn = _get_db()
    row = conn.execute("SELECT * FROM onboarding_state WHERE id = 1").fetchone()
    conn.close()
    return dict(row) if row else {"status": "pending", "turns": 0}


def _set_status(status: str, **fields):
    conn = _get_db()
    sets = ["status = ?"]
    params: list = [status]
    for k, v in fields.items():
        sets.append(f"{k} = ?")
        params.append(v)
    conn.execute(f"UPDATE onboarding_state SET {', '.join(sets)} WHERE id = 1", params)
    conn.commit()
    conn.close()


def start():
    state = get_state()
    if state["status"] == "pending":
        _set_status("in_progress", started_at=time.time())


def mark_turn():
    conn = _get_db()
    conn.execute("UPDATE onboarding_state SET turns = turns + 1 WHERE id = 1")
    conn.commit()
    conn.close()


def complete():
    _set_status("completed", completed_at=time.time())
    log.info("Onboarding completed")


def skip():
    _set_status("skipped", completed_at=time.time())


def reset():
    conn = _get_db()
    conn.execute("UPDATE onboarding_state SET status='pending', turns=0, started_at=NULL, completed_at=NULL WHERE id = 1")
    conn.commit()
    conn.close()


def is_active() -> bool:
    return get_state()["status"] in ("pending", "in_progress")


def missing_fields() -> list[str]:
    profile = get_profile()
    return [f for f, _ in DISCOVERY_FIELDS if not profile.get(f)]


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def onboarding_prompt() -> str:
    """Instructions injected into the system prompt while onboarding is active."""
    if not is_active():
        return ""
    state = get_state()
    profile = get_profile()
    missing = missing_fields()

    if state["status"] == "pending":
        start()

    known = ", ".join(f"{k}={v}" for k, v in profile.items()) or "nothing yet"
    next_topics = ", ".join(d for f, d in DISCOVERY_FIELDS if f in missing[:2]) or "wrap up"

    lines = [
        "ONBOARDING MODE — this is one of your first conversations with this user.",
        "Your goal is to learn about them so you can tailor yourself, NOT to do tasks yet.",
        f"What you already know: {known}.",
        f"Still to learn: {', '.join(missing) if missing else 'nothing — you have enough'}.",
        f"In THIS reply, naturally ask about: {next_topics}.",
        "Ask ONE short, warm question at a time (your usual one-to-two sentences). Do not interrogate.",
        "When the user answers, store it with [ACTION:PROFILE] key ||| value (keys: "
        + ", ".join(f for f, _ in DISCOVERY_FIELDS) + ").",
        "Once you know their goals, use [ACTION:RECOMMEND_SKILLS] <their goals in a few words> to "
        "surface relevant skills, and mention 2-3 by name.",
        "When you have learned their goals and tools, say one welcoming line and emit [ACTION:ONBOARD_DONE].",
    ]
    return "\n".join(lines)


def profile_prompt() -> str:
    """A compact profile summary injected into every prompt after onboarding."""
    profile = get_profile()
    if not profile:
        return ""
    order = ["name", "role", "business", "team_size", "goals", "tools"]
    parts = [f"{k}: {profile[k]}" for k in order if profile.get(k)]
    extra = [f"{k}: {v}" for k, v in profile.items() if k not in order]
    parts += extra
    return "USER PROFILE:\n" + "\n".join(f"- {p}" for p in parts) if parts else ""


def export() -> dict:
    return {"state": get_state(), "profile": get_profile(), "missing": missing_fields()}


# Initialize on import
init_onboarding_db()
