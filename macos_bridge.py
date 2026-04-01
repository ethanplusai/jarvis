"""
JARVIS macOS Bridge — runs NATIVELY on the Mac host.

Exposes HTTP endpoints so the Docker container can call macOS services
(AppleScript, Calendar, Mail, Notes, Terminal, Chrome) via
http://host.docker.internal:8341/...

Start via:  python macos_bridge.py
Or via:     ./start.sh  (starts automatically)

The bridge also acts as a pass-through for executing AppleScript from Docker.
"""

import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Load local .env so we pick up the same keys as the main server
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

# Import JARVIS modules (these only work on macOS)
try:
    from calendar_access import get_todays_events, get_upcoming_events, format_events_for_context, format_schedule_summary
    CALENDAR_OK = True
except Exception:
    CALENDAR_OK = False

try:
    from mail_access import get_unread_count, get_unread_messages, format_unread_summary
    MAIL_OK = True
except Exception:
    MAIL_OK = False

try:
    from notes_access import get_recent_notes, read_note, search_notes_apple, create_apple_note
    NOTES_OK = True
except Exception:
    NOTES_OK = False

try:
    from screen import get_active_windows, format_windows_for_context
    SCREEN_OK = True
except Exception:
    SCREEN_OK = False

try:
    from actions import open_terminal, open_browser, open_claude_in_project
    ACTIONS_OK = True
except Exception:
    ACTIONS_OK = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [bridge] %(message)s")
log = logging.getLogger("bridge")

app = FastAPI(title="JARVIS macOS Bridge", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Health ──────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "online",
        "calendar": CALENDAR_OK,
        "mail": MAIL_OK,
        "notes": NOTES_OK,
        "screen": SCREEN_OK,
        "actions": ACTIONS_OK,
    }


# ── Calendar ─────────────────────────────────────────────────────────

@app.get("/calendar/today")
async def calendar_today():
    if not CALENDAR_OK:
        return {"events": [], "formatted": "Calendar not available.", "error": "not_supported"}
    try:
        events = get_todays_events()
        return {"events": events, "formatted": format_events_for_context(events)}
    except Exception as e:
        return {"events": [], "formatted": "Could not read calendar.", "error": str(e)}


@app.get("/calendar/upcoming")
async def calendar_upcoming():
    if not CALENDAR_OK:
        return {"events": [], "formatted": "Calendar not available.", "error": "not_supported"}
    try:
        events = get_upcoming_events()
        return {"events": events, "formatted": format_events_for_context(events)}
    except Exception as e:
        return {"events": [], "formatted": "Could not read calendar.", "error": str(e)}


# ── Mail ─────────────────────────────────────────────────────────────

@app.get("/mail/unread")
async def mail_unread():
    if not MAIL_OK:
        return {"count": 0, "messages": [], "formatted": "Mail not available.", "error": "not_supported"}
    try:
        count = get_unread_count()
        messages = get_unread_messages(limit=5)
        return {"count": count, "messages": messages, "formatted": format_unread_summary(messages)}
    except Exception as e:
        return {"count": 0, "messages": [], "formatted": "Could not read mail.", "error": str(e)}


# ── Notes ─────────────────────────────────────────────────────────────

@app.get("/notes/recent")
async def notes_recent():
    if not NOTES_OK:
        return {"notes": [], "error": "not_supported"}
    try:
        notes = get_recent_notes(limit=5)
        return {"notes": notes}
    except Exception as e:
        return {"notes": [], "error": str(e)}


@app.get("/notes/search")
async def notes_search(q: str):
    if not NOTES_OK:
        return {"notes": [], "error": "not_supported"}
    try:
        notes = search_notes_apple(q)
        return {"notes": notes}
    except Exception as e:
        return {"notes": [], "error": str(e)}


class CreateNoteRequest(BaseModel):
    title: str
    body: str


@app.post("/notes/create")
async def notes_create(req: CreateNoteRequest):
    if not NOTES_OK:
        return {"success": False, "error": "not_supported"}
    try:
        await create_apple_note(req.title, req.body)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Screen ────────────────────────────────────────────────────────────

@app.get("/screen/windows")
async def screen_windows():
    if not SCREEN_OK:
        return {"windows": [], "formatted": "Screen access not available.", "error": "not_supported"}
    try:
        windows = get_active_windows()
        return {"windows": windows, "formatted": format_windows_for_context(windows)}
    except Exception as e:
        return {"windows": [], "formatted": "Could not read screen.", "error": str(e)}


# ── Actions ────────────────────────────────────────────────────────────

class AppleScriptRequest(BaseModel):
    script: str


@app.post("/applescript")
async def run_applescript(req: AppleScriptRequest):
    """Execute AppleScript on the host Mac. Used by Docker container."""
    try:
        result = subprocess.run(
            ["osascript", "-e", req.script],
            capture_output=True, text=True, timeout=15,
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except Exception as e:
        return {"success": False, "stdout": "", "stderr": str(e)}


class OpenTerminalRequest(BaseModel):
    command: Optional[str] = "claude --dangerously-skip-permissions"
    working_dir: Optional[str] = None


@app.post("/actions/terminal")
async def action_terminal(req: OpenTerminalRequest):
    if not ACTIONS_OK:
        return {"success": False, "error": "not_supported"}
    try:
        result = await open_terminal(req.command or "claude --dangerously-skip-permissions", req.working_dir)
        return {"success": True, "result": result}
    except Exception as e:
        return {"success": False, "error": str(e)}


class OpenBrowserRequest(BaseModel):
    url: str
    browser: str = "chrome"


@app.post("/actions/browser")
async def action_browser(req: OpenBrowserRequest):
    if not ACTIONS_OK:
        return {"success": False, "error": "not_supported"}
    try:
        await open_browser(req.url, req.browser)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Entrypoint ────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("BRIDGE_PORT", "8341"))
    print()
    print("  JARVIS macOS Bridge v1.0")
    print(f"  Listening on http://0.0.0.0:{port}")
    print(f"  Calendar: {'✓' if CALENDAR_OK else '✗'}  Mail: {'✓' if MAIL_OK else '✗'}  "
          f"Notes: {'✓' if NOTES_OK else '✗'}  Actions: {'✓' if ACTIONS_OK else '✗'}")
    print()
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
