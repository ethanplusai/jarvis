"""
JARVIS Server — Voice AI + Development Orchestration

Handles:
1. WebSocket voice interface (browser audio <-> LLM <-> TTS)
2. Claude Code task manager (spawn/manage claude -p subprocesses)
3. Project awareness (scan Desktop for git repos)
4. REST API for task management
"""

import asyncio
import base64
import json
import logging
import os
import secrets
import sys
import time
from pathlib import Path

# Load .env file if present
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import anthropic
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ab_testing import ABTester
from action_handlers import (
    execute_browse as _execute_browse,
)
from action_handlers import (
    execute_open_terminal as _execute_open_terminal,
)
from action_handlers import (
    handle_open_terminal,
    handle_show_recent,
    recently_built,
)
from actions import (
    _generate_project_name,
    open_browser,
)
from api_settings import build_settings_router
from dispatch_registry import DispatchRegistry
from fast_actions import detect_action_fast  # noqa: F401 — re-exported for tests
from formatting import (
    apply_speech_corrections,  # noqa: F401 — re-exported for tests
    extract_action,  # noqa: F401 — re-exported for tests
    format_mc_decisions_for_voice,
    format_mc_inbox_for_voice,
    format_mc_tasks_for_voice,
    format_projects_for_prompt,  # noqa: F401 — re-exported for tests
    strip_markdown_for_tts,
)
from learning import UsageLearner
from llm import generate_response as _llm_generate_response
from lookups import (
    do_calendar_lookup,
    do_mail_lookup,
    do_screen_lookup,
    get_lookup_status,
)
from lookups import (
    lookup_and_report as _lookup_and_report,
)
from mc_client import mc_client
from memory import (
    create_note,
    extract_memories,
    get_important_memories,
    get_open_tasks,
    remember,
)
from models import TaskRequest
from notes_access import create_apple_note, read_note
from planner import BYPASS_PHRASES, TaskPlanner
from qa import QAAgent
from sanitize import (
    ALLOW_REMOTE_CONTROL,
    DANGEROUS_FLAG,
    DANGEROUS_FLAG_LIST,
    escape_applescript,
    escape_shell_in_applescript,
)
from screen import format_windows_for_context
from suggestions import suggest_followup
from task_manager import ClaudeTaskManager
from tracking import SuccessTracker
from tts import synthesize_speech
from usage import (
    SESSION_START as _session_start,
)
from usage import (
    SESSION_TOKENS as _session_tokens,
)
from usage import (
    append_usage_entry as _append_usage_entry,  # noqa: F401
)
from usage import (
    cost_from_tokens as _cost_from_tokens,  # noqa: F401
)
from usage import (
    get_usage_for_period as _get_usage_for_period,
)
from usage import (
    get_usage_summary,
)
from work_mode import WorkSession, is_casual_question, session_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("jarvis")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
FISH_API_KEY = os.getenv("FISH_API_KEY", "")
FISH_VOICE_ID = os.getenv("FISH_VOICE_ID", "612b878b113047d9a770c069c8b4fdfe")  # JARVIS (MCU)
FISH_API_URL = "https://api.fish.audio/v1/tts"
USER_NAME = os.getenv("USER_NAME", "sir")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

DESKTOP_PATH = Path.home() / "Desktop"


# ---------------------------------------------------------------------------
# Weather (wttr.in)
# ---------------------------------------------------------------------------

_cached_weather: str | None = None
_weather_fetched: bool = False


async def fetch_weather() -> str:
    """Fetch current weather from wttr.in. Cached for the session."""
    global _cached_weather, _weather_fetched
    if _weather_fetched:
        return _cached_weather or "Weather data unavailable."
    _weather_fetched = True
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.get("https://wttr.in/?format=%l:+%C,+%t", headers={"User-Agent": "curl"})
            if resp.status_code == 200:
                _cached_weather = resp.text.strip()
                return _cached_weather
    except Exception as e:
        log.warning(f"Weather fetch failed: {e}")
    _cached_weather = None
    return "Weather data unavailable."


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Shared state (module-level singletons used across handlers)
# ---------------------------------------------------------------------------

qa_agent = QAAgent()
success_tracker = SuccessTracker()
ab_tester = ABTester()
usage_learner = UsageLearner()
task_manager = ClaudeTaskManager(
    max_concurrent=3,
    qa_agent=qa_agent,
    success_tracker=success_tracker,
    suggest_followup=suggest_followup,
)
anthropic_client: anthropic.AsyncAnthropic | None = None
cached_projects: list[dict] = []
dispatch_registry = DispatchRegistry()

# Background context cache — never blocks responses
_ctx_cache = {
    "screen": "",
    "calendar": "No calendar data yet.",
    "mail": "No mail data yet.",
    "weather": "Weather data unavailable.",
}


async def generate_response(
    text: str,
    client,
    task_mgr,
    projects: list[dict],
    conversation_history: list[dict],
    last_response: str = "",
    session_summary: str = "",
) -> str:
    """Server-local adapter injecting shared state into llm.generate_response."""
    return await _llm_generate_response(
        text=text,
        client=client,
        task_mgr=task_mgr,
        projects=projects,
        conversation_history=conversation_history,
        ctx_cache=_ctx_cache,
        dispatch_registry=dispatch_registry,
        user_name=USER_NAME,
        project_dir=PROJECT_DIR,
        last_response=last_response,
        session_summary=session_summary,
        lookup_status=get_lookup_status(),
    )


# ---------------------------------------------------------------------------
# Project Scanner
# ---------------------------------------------------------------------------


async def scan_projects() -> list[dict]:
    """Quick scan of ~/Desktop for git repos (depth 1)."""
    projects = []
    desktop = DESKTOP_PATH

    if not desktop.exists():
        return projects

    try:
        for entry in sorted(desktop.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            git_dir = entry / ".git"
            if git_dir.exists():
                branch = "unknown"
                head_file = git_dir / "HEAD"
                try:
                    head_content = head_file.read_text().strip()
                    if head_content.startswith("ref: refs/heads/"):
                        branch = head_content.replace("ref: refs/heads/", "")
                except Exception:
                    pass

                projects.append(
                    {
                        "name": entry.name,
                        "path": str(entry),
                        "branch": branch,
                    }
                )
    except (PermissionError, FileNotFoundError):
        pass

    return projects


async def _execute_research(target: str, ws=None):
    """Execute research via claude -p in background. Opens report and speaks when done."""
    try:
        name = _generate_project_name(target)
        path = str(Path.home() / "Desktop" / name)
        os.makedirs(path, exist_ok=True)

        prompt = (
            f"{target}\n\n"
            f"Research this thoroughly. Find REAL data — not made-up examples.\n"
            f"Create a well-designed HTML file called `report.html` in the current directory.\n"
            f"Dark theme, clean typography, organized sections, real links and sources.\n"
            f"The working directory is: {path}"
        )

        log.info(f"Research started via claude -p in {path}")

        process = await asyncio.create_subprocess_exec(
            "claude",
            "-p",
            "--output-format",
            "text",
            *DANGEROUS_FLAG_LIST,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=path,
        )

        stdout, stderr = await asyncio.wait_for(
            process.communicate(input=prompt.encode()),
            timeout=300,
        )

        result = stdout.decode().strip()
        log.info(f"Research complete ({len(result)} chars)")

        recently_built.append({"name": name, "path": path, "time": time.time()})

        # Find and open any HTML report
        report = Path(path) / "report.html"
        if not report.exists():
            # Check for any HTML file
            html_files = list(Path(path).glob("*.html"))
            if html_files:
                report = html_files[0]

        if report.exists():
            await open_browser(f"file://{report}")
            log.info(f"Opened {report.name} in browser")

        # Notify via voice if WebSocket still connected
        if ws:
            try:
                notify_text = "Research is complete, sir. Report is open in your browser."
                audio = await synthesize_speech(notify_text)
                if audio:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": notify_text})
                    await ws.send_json({"type": "status", "state": "idle"})
                    log.info(f"JARVIS: {notify_text}")
            except Exception:
                pass  # WebSocket might be gone

    except TimeoutError:
        log.error("Research timed out after 5 minutes")
        if ws:
            try:
                audio = await synthesize_speech("Research timed out, sir. It was taking too long.")
                if audio:
                    await ws.send_json(
                        {"type": "audio", "data": base64.b64encode(audio).decode(), "text": "Research timed out, sir."}
                    )
            except Exception:
                pass
    except Exception as e:
        log.error(f"Research execution failed: {e}")


async def _focus_terminal_window(project_name: str):
    """Bring a Terminal window for the project to front.

    Uses tmux attach when a session exists, falls back to AppleScript window search.
    """
    # Try tmux first
    session = session_manager.find_session(project_name)
    if session and await session.is_alive():
        await session_manager.attach_in_terminal(session.name)
        return

    # Fallback: search Terminal windows by name
    escaped = escape_applescript(project_name)
    script = f'''
tell application "Terminal"
    repeat with w in windows
        if name of w contains "{escaped}" then
            set index of w to 1
            activate
            exit repeat
        end if
    end repeat
end tell
'''
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5)
    except Exception:
        pass


def _find_project_dir(project_name: str) -> str | None:
    """Find a project directory by name from cached projects or Desktop."""
    for p in cached_projects:
        if project_name.lower() in p.get("name", "").lower():
            return p.get("path")
    # Check common project locations
    search_dirs = [
        Path.home() / "Desktop",
        Path.home() / "Documents",
        Path.home() / "IdeaProjects",
        Path.home() / "Projects",
    ]
    for search_dir in search_dirs:
        # Direct path check — works even when macOS TCC blocks directory listing
        direct = search_dir / project_name
        if direct.is_dir():
            return str(direct)
        # Fall back to directory scan for fuzzy/partial matches
        try:
            for d in search_dir.iterdir():
                if d.is_dir() and project_name.lower() in d.name.lower():
                    return str(d)
        except (PermissionError, FileNotFoundError):
            continue
    return None


async def _execute_prompt_project(
    project_name: str,
    prompt: str,
    work_session: WorkSession,
    ws,
    dispatch_id: int | None = None,
    history: list[dict] | None = None,
    voice_state: dict | None = None,
):
    """Dispatch a prompt to Claude Code in a project directory.

    Runs entirely in the background. JARVIS returns to conversation mode
    immediately. When Claude Code finishes, JARVIS interrupts to report.
    """
    try:
        project_dir = _find_project_dir(project_name)

        # Register dispatch if not already registered
        if dispatch_id is None:
            dispatch_id = dispatch_registry.register(project_name, project_dir or "", prompt)

        if not project_dir:
            msg = f"Couldn't find the {project_name} project directory, sir."
            audio = await synthesize_speech(msg)
            if audio and ws:
                try:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                except Exception:
                    pass
            return

        # Use a SEPARATE session so we don't trap the main conversation
        dispatch = WorkSession()
        await dispatch.start(project_dir, project_name)

        # Bring matching Terminal window to front so user can watch
        asyncio.create_task(_focus_terminal_window(project_name))

        log.info(f"Dispatching to {project_name} in {project_dir}: {prompt[:80]}")
        dispatch_registry.update_status(dispatch_id, "building")

        # Run claude -p in background
        full_response = await dispatch.send(prompt)
        await dispatch.stop()

        # Auto-open any localhost URLs from response
        import re as _re

        # Check for the explicit RUNNING_AT marker first
        running_match = _re.search(r"RUNNING_AT=(https?://localhost:\d+)", full_response or "")
        if not running_match:
            running_match = _re.search(r"https?://localhost:\d+", full_response or "")
        if running_match:
            url = running_match.group(1) if running_match.lastindex else running_match.group(0)
            asyncio.create_task(_execute_browse(url))
            log.info(f"Auto-opening {url}")
            # Store URL in dispatch
            if dispatch_id:
                dispatch_registry.update_status(
                    dispatch_id, "completed", response=full_response[:2000], summary=f"Running at {url}"
                )

        if not full_response or full_response.startswith("Hit a problem") or full_response.startswith("That's taking"):
            dispatch_registry.update_status(
                dispatch_id, "failed" if full_response else "timeout", response=full_response or ""
            )
            msg = f"Sir, I ran into an issue with {project_name}. {full_response[:150] if full_response else 'No response received.'}"
        else:
            # Summarize via Haiku — don't read word for word
            if anthropic_client:
                try:
                    summary = await anthropic_client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=150,
                        system=(
                            "You are JARVIS reporting back on what you found or built in a project. "
                            "Speak in first person — 'I found', 'I built', 'I reviewed'. "
                            "Start with 'Sir, ' to get the user's attention. "
                            "Be specific but concise — highlight the key findings or actions taken. "
                            "If there are multiple items, give the count and top 2-3 briefly. "
                            "End by asking how the user wants to proceed. "
                            "NEVER read out URLs or localhost addresses. NEVER say 'Claude Code'. "
                            "2-3 sentences max. No markdown. Natural spoken voice."
                        ),
                        messages=[
                            {
                                "role": "user",
                                "content": f"Project: {project_name}\nClaude Code reported:\n{full_response[:3000]}",
                            }
                        ],
                    )
                    msg = summary.content[0].text
                except Exception:
                    msg = f"Sir, {project_name} finished. Here's the gist: {full_response[:200]}"
            else:
                msg = f"Sir, {project_name} is done. {full_response[:200]}"

        # Speak the result — skip if user has spoken recently to avoid audio collision
        log.info(f"Dispatch summary for {project_name}: {msg[:100]}")
        if voice_state and time.time() - voice_state["last_user_time"] < 3:
            log.info(f"Skipping dispatch audio for {project_name} — user spoke recently")
            # Result is still stored in history below so JARVIS can reference it
        else:
            audio = await synthesize_speech(strip_markdown_for_tts(msg))
            if ws:
                try:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    if audio:
                        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                        log.info(f"Dispatch audio sent for {project_name}")
                    else:
                        await ws.send_json({"type": "text", "text": msg})
                        log.info(f"Dispatch text fallback sent for {project_name}")
                except Exception as e:
                    log.error(f"Dispatch audio send failed: {e}")

        # Store dispatch result in conversation history so JARVIS remembers it
        if history is not None:
            history.append({"role": "assistant", "content": f"[Dispatch result for {project_name}]: {msg}"})

        dispatch_registry.update_status(dispatch_id, "completed", response=full_response[:2000], summary=msg[:200])
        log.info(f"Project {project_name} dispatch complete ({len(full_response)} chars)")

    except Exception as e:
        log.error(f"Prompt project failed: {e}", exc_info=True)
        try:
            msg = f"Had trouble connecting to {project_name}, sir."
            audio = await synthesize_speech(msg)
            if audio and ws:
                await ws.send_json({"type": "status", "state": "speaking"})
                await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
        except Exception:
            pass


async def self_work_and_notify(session: WorkSession, prompt: str, ws):
    """Run claude -p in background and notify via voice when done."""
    try:
        full_response = await session.send(prompt)
        log.info(f"Background work complete ({len(full_response)} chars)")

        # Summarize and speak
        if anthropic_client and full_response:
            try:
                summary = await anthropic_client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=100,
                    system="You are JARVIS. Summarize what you just completed in 1 sentence. First person — 'I built', 'I set up'. No markdown. Never say 'Claude Code'.",
                    messages=[{"role": "user", "content": f"Claude Code completed:\n{full_response[:2000]}"}],
                )
                msg = summary.content[0].text
            except Exception:
                msg = "Work is complete, sir."

            try:
                audio = await synthesize_speech(msg)
                if audio:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                    await ws.send_json({"type": "status", "state": "idle"})
                    log.info(f"JARVIS: {msg}")
            except Exception:
                pass
    except Exception as e:
        log.error(f"Background work failed: {e}")


# Smart greeting — track last greeting to avoid re-greeting on reconnect
_last_greeting_time: float = 0


def _refresh_context_sync():
    """Run in a SEPARATE THREAD — refreshes screen/calendar/mail context.

    This runs completely off the async event loop so it never blocks responses.
    """
    import threading

    def _worker():
        while True:
            try:
                # Screen — fast
                try:
                    proc = __import__("subprocess").run(
                        [
                            "osascript",
                            "-e",
                            """
set windowList to ""
tell application "System Events"
    set frontApp to name of first application process whose frontmost is true
    set visibleApps to every application process whose visible is true
    repeat with proc in visibleApps
        set appName to name of proc
        try
            set winCount to count of windows of proc
            if winCount > 0 then
                repeat with w in (windows of proc)
                    try
                        set winTitle to name of w
                        if winTitle is not "" and winTitle is not missing value then
                            set windowList to windowList & appName & "|||" & winTitle & "|||" & (appName = frontApp) & linefeed
                        end if
                    end try
                end repeat
            end if
        end try
    end repeat
end tell
return windowList
""",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if proc.returncode == 0 and proc.stdout.strip():
                        windows = []
                        for line in proc.stdout.strip().split("\n"):
                            parts = line.strip().split("|||")
                            if len(parts) >= 3:
                                windows.append(
                                    {
                                        "app": parts[0].strip(),
                                        "title": parts[1].strip(),
                                        "frontmost": parts[2].strip().lower() == "true",
                                    }
                                )
                        if windows:
                            _ctx_cache["screen"] = format_windows_for_context(windows)
                except Exception:
                    pass

            except Exception as e:
                log.debug(f"Context thread error: {e}")

            # Weather — refresh every loop (30s is fine, API is fast)
            try:
                import json as _json
                import urllib.request

                url = "https://api.open-meteo.com/v1/forecast?latitude=27.77&longitude=-82.64&current=temperature_2m,weathercode&temperature_unit=fahrenheit"
                with urllib.request.urlopen(url, timeout=3) as resp:
                    d = _json.loads(resp.read()).get("current", {})
                    temp = d.get("temperature_2m", "?")
                    _ctx_cache["weather"] = f"Current weather in St. Petersburg, FL: {temp}°F"
            except Exception:
                pass

            time.sleep(30)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    log.info("Context refresh thread started")


_AUTH_TOKEN: str = ""


async def _mc_inbox_watcher():
    """Poll Mission Control inbox for new agent reports and notify the user."""
    seen_ids: set[str] = set()
    while True:
        try:
            await asyncio.sleep(15)
            messages = await mc_client.list_inbox(agent="me", status="unread", limit=20)
            for msg in messages:
                msg_id = msg.get("id")
                if not msg_id or msg_id in seen_ids:
                    continue
                seen_ids.add(msg_id)
                msg_type = msg.get("type", "update")
                sender = msg.get("from", "system")
                subject = msg.get("subject", "(no subject)")
                if msg_type == "report":
                    log.info(f"[MC inbox] {sender} finished: {subject}")
                    notification = f"Sir, {sender} finished: {subject}"
                    await task_manager._notify(
                        {"type": "mc_inbox", "from": sender, "subject": subject, "body": notification}
                    )
                elif msg_type == "question":
                    log.info(f"[MC inbox] {sender} is asking: {subject}")
                    await task_manager._notify(
                        {"type": "mc_inbox", "from": sender, "subject": subject, "body": msg.get("body", "")[:200]}
                    )
                # Mark as read so we don't re-notify
                await mc_client.mark_inbox_read(msg_id)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.debug(f"Inbox watcher error: {e}")


@asynccontextmanager
async def lifespan(application: FastAPI):
    global anthropic_client, cached_projects, _AUTH_TOKEN
    _AUTH_TOKEN = secrets.token_urlsafe(32)
    print(f"  Auth token: {_AUTH_TOKEN[:8]}... (use /auth/token endpoint for full token)")
    if ANTHROPIC_API_KEY:
        anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    else:
        log.warning("ANTHROPIC_API_KEY not set — LLM features disabled")
    cached_projects = []

    # Start context refresh in a separate thread (never touches event loop)
    _refresh_context_sync()

    # Start MC daemon if MC is reachable
    if await mc_client.is_healthy():
        status = await mc_client.get_daemon_status()
        if status and not status.get("isRunning"):
            result = await mc_client.start_daemon()
            if result:
                log.info("Mission Control daemon started")
            else:
                log.warning("Failed to start MC daemon")
        else:
            log.info("Mission Control daemon already running")
    else:
        log.info("Mission Control not reachable — tasks will use fallback dispatch")

    # Start MC inbox watcher (notifies user when MC agents finish tasks)
    inbox_task = asyncio.create_task(_mc_inbox_watcher())

    log.info("JARVIS server starting")

    yield

    inbox_task.cancel()


app = FastAPI(title="JARVIS Server", version="0.1.0", lifespan=lifespan)

# Rate limiting — defense in depth. Single-user localhost means 60/min is generous.
# If you expose JARVIS to a network, reduce this or add per-route limits.
from slowapi import Limiter, _rate_limit_exceeded_handler  # noqa: E402
from slowapi.errors import RateLimitExceeded  # noqa: E402
from slowapi.util import get_remote_address  # noqa: E402

limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

_ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:8340",
    "https://localhost:8340",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:8340",
    "https://127.0.0.1:8340",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type"],
)


# -- Auth ------------------------------------------------------------------


async def require_auth(authorization: str = Header(None)):
    """Require Bearer token on protected endpoints."""
    if not _AUTH_TOKEN:
        return
    if authorization != f"Bearer {_AUTH_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/auth/token")
async def get_auth_token():
    """Return the auth token. Only accessible from same-origin (CORS-protected)."""
    return {"token": _AUTH_TOKEN}


# -- REST Endpoints --------------------------------------------------------


@app.get("/api/health")
async def health():
    return {"status": "online", "name": "JARVIS", "version": "0.1.0"}


@app.get("/api/tts-test", dependencies=[Depends(require_auth)])
async def tts_test():
    """Generate a test audio clip for debugging."""
    audio = await synthesize_speech("Testing audio, sir.")
    if audio:
        return {"audio": base64.b64encode(audio).decode()}
    return {"audio": None, "error": "TTS failed"}


@app.get("/api/usage", dependencies=[Depends(require_auth)])
async def api_usage():
    uptime = int(time.time() - _session_start)
    today = _get_usage_for_period(86400)
    week = _get_usage_for_period(86400 * 7)
    month = _get_usage_for_period(86400 * 30)
    all_time = _get_usage_for_period(None)
    return {
        "session": {**_session_tokens, "uptime_seconds": uptime},
        "today": {**today, "cost_usd": round(_cost_from_tokens(today["input_tokens"], today["output_tokens"]), 4)},
        "week": {**week, "cost_usd": round(_cost_from_tokens(week["input_tokens"], week["output_tokens"]), 4)},
        "month": {**month, "cost_usd": round(_cost_from_tokens(month["input_tokens"], month["output_tokens"]), 4)},
        "all_time": {
            **all_time,
            "cost_usd": round(_cost_from_tokens(all_time["input_tokens"], all_time["output_tokens"]), 4),
        },
    }


@app.get("/api/tasks", dependencies=[Depends(require_auth)])
async def api_list_tasks():
    tasks = await task_manager.list_tasks()
    return {"tasks": [t.to_dict() for t in tasks]}


@app.get("/api/tasks/{task_id}", dependencies=[Depends(require_auth)])
async def api_get_task(task_id: str):
    task = await task_manager.get_status(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"error": "Task not found"})
    return {"task": task.to_dict()}


@app.post("/api/tasks", dependencies=[Depends(require_auth)])
async def api_create_task(req: TaskRequest):
    try:
        task_id = await task_manager.spawn(req.prompt, req.working_dir)
        return {"task_id": task_id, "status": "spawned"}
    except RuntimeError as e:
        return JSONResponse(status_code=429, content={"error": str(e)})


@app.delete("/api/tasks/{task_id}", dependencies=[Depends(require_auth)])
async def api_cancel_task(task_id: str):
    cancelled = await task_manager.cancel(task_id)
    if not cancelled:
        return JSONResponse(
            status_code=404,
            content={"error": "Task not found or not cancellable"},
        )
    return {"task_id": task_id, "status": "cancelled"}


@app.get("/api/projects", dependencies=[Depends(require_auth)])
async def api_list_projects():
    global cached_projects
    cached_projects = await scan_projects()
    return {"projects": cached_projects}


@app.get("/api/sessions", dependencies=[Depends(require_auth)])
async def api_list_sessions():
    sessions = await session_manager.list_sessions()
    return {"sessions": sessions}


@app.get("/api/memory", dependencies=[Depends(require_auth)])
async def api_memory():
    memories = get_important_memories(limit=20)
    tasks = get_open_tasks()
    return {"memories": memories, "tasks": tasks}


@app.get("/api/dispatches", dependencies=[Depends(require_auth)])
async def api_dispatches():
    active = dispatch_registry.get_active()
    recent = dispatch_registry.get_recent(limit=10)
    return {"active": active, "recent": recent}


# -- Fast Action Detection (no LLM call) -----------------------------------


def _scan_projects_sync() -> list[dict]:
    """Scan common project directories — runs in executor."""
    projects = []
    search_dirs = [
        Path.home() / "Desktop",
        Path.home() / "Documents",
        Path.home() / "IdeaProjects",
        Path.home() / "Projects",
    ]
    for search_dir in search_dirs:
        try:
            for entry in search_dir.iterdir():
                if entry.is_dir() and not entry.name.startswith("."):
                    projects.append({"name": entry.name, "path": str(entry), "branch": ""})
        except PermissionError:
            continue
        except Exception as e:
            log.debug(f"Project scan error in {search_dir}: {e}")
            continue
    return projects


# -- Action Handlers -------------------------------------------------------
# handle_open_terminal / handle_build / handle_show_recent live in
# action_handlers.py — imported at top of this module.


# ---------------------------------------------------------------------------
# Background lookup system — see lookups.py for implementation.
# ---------------------------------------------------------------------------


async def _do_calendar_lookup() -> str:
    return await do_calendar_lookup(_ctx_cache)


async def _do_mail_lookup() -> str:
    return await do_mail_lookup(_ctx_cache)


async def _do_screen_lookup() -> str:
    return await do_screen_lookup(anthropic_client)


async def handle_browse(text: str, target: str) -> str:
    """Open a URL directly or search. Smart about detecting URLs in speech."""
    import re
    from urllib.parse import quote

    browser = "firefox" if "firefox" in text.lower() else "chrome"

    # 1. Try to find a URL or domain in the text
    # Match things like "joetmd.com", "google.com/maps", "https://example.com"
    url_pattern = r"(?:https?://)?(?:www\.)?([a-zA-Z0-9][-a-zA-Z0-9]*(?:\.[a-zA-Z]{2,})+(?:/[^\s]*)?)"
    url_match = re.search(url_pattern, text, re.IGNORECASE)

    if url_match:
        domain = url_match.group(0)
        if not domain.startswith("http"):
            domain = "https://" + domain
        await open_browser(domain, browser)
        return f"Opened {url_match.group(0)}, sir."

    # 2. Check for spoken domains that speech-to-text mangled
    # "Joe tmd.com" → "joetmd.com", "roofo.co" etc.
    # Try joining words that end/start with a dot pattern
    words = text.split()
    for _i, word in enumerate(words):
        # Look for word ending with common TLD
        if re.search(r"\.(com|co|io|ai|org|net|dev|app)$", word, re.IGNORECASE):
            # This word IS a domain — might have spaces before it
            domain = word
            # Check if previous word should be joined (e.g., "Joe tmd.com" → "joetmd.com" is tricky)
            if not domain.startswith("http"):
                domain = "https://" + domain
            await open_browser(domain, browser)
            return f"Opened {word}, sir."

    # 3. Fall back to Google search with cleaned query
    query = target
    for prefix in [
        "search for",
        "look up",
        "google",
        "find me",
        "pull up",
        "open chrome",
        "open firefox",
        "open browser",
        "go to",
        "can you",
        "in the browser",
        "can you go to",
        "please",
    ]:
        query = query.lower().replace(prefix, "").strip()
    # Remove filler words
    query = re.sub(r"\b(can|you|the|in|to|a|an|for|me|my|please)\b", "", query).strip()
    query = re.sub(r"\s+", " ", query).strip()

    if not query:
        query = target

    url = f"https://www.google.com/search?q={quote(query)}"
    await open_browser(url, browser)
    return "Searching for that, sir."


async def handle_research(text: str, target: str, client: anthropic.AsyncAnthropic) -> str:
    """Deep research with Opus — write results to HTML, open in browser."""
    try:
        research_response = await client.messages.create(
            model="claude-opus-4-6",
            max_tokens=2000,
            system=f"You are JARVIS, researching a topic for {USER_NAME}. Be thorough, organized, and cite sources where possible.",
            messages=[{"role": "user", "content": f"Research this thoroughly:\n\n{target}"}],
        )
        research_text = research_response.content[0].text

        import html as _html

        html_content = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>JARVIS Research: {_html.escape(target[:60])}</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 800px; margin: 40px auto; padding: 20px; background: #0a0a0a; color: #e0e0e0; line-height: 1.7; }}
h1 {{ color: #0ea5e9; font-size: 1.4em; border-bottom: 1px solid #222; padding-bottom: 10px; }}
h2 {{ color: #38bdf8; font-size: 1.1em; margin-top: 24px; }}
a {{ color: #0ea5e9; }}
pre {{ background: #111; padding: 12px; border-radius: 6px; overflow-x: auto; }}
code {{ background: #111; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }}
blockquote {{ border-left: 3px solid #0ea5e9; margin-left: 0; padding-left: 16px; color: #aaa; }}
</style>
</head><body>
<h1>Research: {_html.escape(target[:80])}</h1>
<div>{research_text.replace(chr(10), "<br>")}</div>
<hr style="border-color:#222;margin-top:40px">
<p style="color:#555;font-size:0.8em">Researched by JARVIS using Claude Opus &bull; {datetime.now().strftime("%B %d, %Y %I:%M %p")}</p>
</body></html>"""

        results_file = Path.home() / "Desktop" / ".jarvis_research.html"
        results_file.write_text(html_content)

        browser_name = "firefox" if "firefox" in text.lower() else "chrome"
        await open_browser(f"file://{results_file}", browser_name)

        # Short voice summary via Haiku
        summary = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            system="Summarize this research in ONE sentence for voice. No markdown.",
            messages=[{"role": "user", "content": research_text[:2000]}],
        )
        return summary.content[0].text + " Full results are in your browser, sir."

    except Exception as e:
        log.error(f"Research failed: {e}")
        from urllib.parse import quote

        await open_browser(f"https://www.google.com/search?q={quote(target)}")
        return "Pulled up a search for that, sir."


# -- Session Summary (Three-Tier Memory) -----------------------------------


async def _update_session_summary(
    old_summary: str,
    rotated_messages: list[dict],
    client: anthropic.AsyncAnthropic,
) -> str:
    """Background Haiku call to update the rolling session summary."""
    prompt = f"""Update this conversation summary to include the new messages.

Current summary: {old_summary or "(start of conversation)"}

New messages to incorporate:
{chr(10).join(f"{m['role']}: {m['content'][:200]}" for m in rotated_messages)}

Write an updated summary in 2-4 sentences capturing the key topics, decisions, and context. Be concise."""

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        log.warning(f"Summary update failed: {e}")
        return old_summary  # Keep old summary on failure


# -- WebSocket Voice Handler -----------------------------------------------


@app.websocket("/ws/voice")
async def voice_handler(ws: WebSocket):
    """
    WebSocket protocol:

    Client -> Server:
        {"type": "transcript", "text": "...", "isFinal": true}

    Server -> Client:
        {"type": "audio", "data": "<base64 mp3>", "text": "spoken text"}
        {"type": "status", "state": "thinking"|"speaking"|"idle"|"working"}
        {"type": "task_spawned", "task_id": "...", "prompt": "..."}
        {"type": "task_complete", "task_id": "...", "summary": "..."}
    """
    # Validate auth token from query params
    token = ws.query_params.get("token", "")
    if _AUTH_TOKEN and token != _AUTH_TOKEN:
        await ws.close(code=4001, reason="Unauthorized")
        return
    await ws.accept()
    task_manager.register_websocket(ws)
    history: list[dict] = []
    work_session = WorkSession()
    planner = TaskPlanner()

    # Response cancellation — when new input arrives, cancel current response
    _current_response_id = 0
    _cancel_response = False

    # Audio collision prevention — track when user last spoke
    voice_state = {"last_user_time": 0.0}

    # Self-awareness — track last spoken response to avoid repetition
    last_jarvis_response = ""

    # Three-tier conversation memory
    session_buffer: list[dict] = []  # ALL messages, never truncated
    session_summary: str = ""  # Rolling summary of older conversation
    summary_update_pending: bool = False
    messages_since_last_summary: int = 0

    log.info("Voice WebSocket connected")

    try:
        # ── Greeting — always start in conversation mode ──
        now = datetime.now()
        hour = now.hour
        if hour < 12:
            greeting = "Good morning, sir."
        elif hour < 17:
            greeting = "Good afternoon, sir."
        else:
            greeting = "Good evening, sir."

        global _last_greeting_time
        should_greet = (time.time() - _last_greeting_time) > 60

        if should_greet:
            _last_greeting_time = time.time()

            async def _send_greeting():
                try:
                    audio_bytes = await synthesize_speech(greeting)
                    if audio_bytes:
                        encoded = base64.b64encode(audio_bytes).decode()
                        await ws.send_json({"type": "status", "state": "speaking"})
                        await ws.send_json({"type": "audio", "data": encoded, "text": greeting})
                        history.append({"role": "assistant", "content": greeting})
                        log.info(f"JARVIS: {greeting}")
                        await ws.send_json({"type": "status", "state": "idle"})
                except Exception as e:
                    log.warning(f"Greeting failed: {e}")

            asyncio.create_task(_send_greeting())

        try:
            await ws.send_json({"type": "status", "state": "idle"})
        except Exception:
            return  # WebSocket already gone

        _VALID_WS_TYPES = {"transcript", "fix_self"}
        _MAX_TEXT_LENGTH = 10000

        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.warning(f"Malformed WebSocket JSON: {raw[:200]}")
                continue

            # Validate message type
            msg_type = msg.get("type")
            if not isinstance(msg_type, str) or msg_type not in _VALID_WS_TYPES:
                if msg_type is not None:
                    log.warning(f"Unknown WebSocket message type: {msg_type}")
                continue

            # ── Fix-self: activate work mode in JARVIS repo ──
            if msg_type == "fix_self":
                jarvis_dir = str(Path(__file__).parent)
                await work_session.start(jarvis_dir)
                response_text = "Work mode active in my own repo, sir. Tell me what needs fixing."
                tts = strip_markdown_for_tts(response_text)
                await ws.send_json({"type": "status", "state": "speaking"})
                audio = await synthesize_speech(tts)
                if audio:
                    await ws.send_json({"type": "audio", "data": audio, "text": response_text})
                else:
                    await ws.send_json({"type": "text", "text": response_text})
                continue

            # transcript type — validate fields
            if not msg.get("isFinal"):
                continue

            text = msg.get("text", "")
            if not isinstance(text, str) or len(text) > _MAX_TEXT_LENGTH:
                log.warning(
                    f"Invalid transcript: type={type(text).__name__}, len={len(text) if isinstance(text, str) else 'N/A'}"
                )
                continue

            user_text = apply_speech_corrections(text.strip())
            if not user_text:
                continue

            # Cancel any in-flight response
            _current_response_id += 1
            _cancel_response = True
            await asyncio.sleep(0.05)  # Let any pending sends notice the cancellation
            _cancel_response = False

            voice_state["last_user_time"] = time.time()
            log.info(f"User: {user_text}")
            await ws.send_json({"type": "status", "state": "thinking"})

            # Lazy project scan on first message
            global cached_projects
            if not cached_projects:
                try:
                    # Run in executor since scan_projects does sync file I/O
                    loop = asyncio.get_event_loop()
                    cached_projects = await asyncio.wait_for(loop.run_in_executor(None, _scan_projects_sync), timeout=3)
                    log.info(f"Scanned {len(cached_projects)} projects")
                except Exception:
                    cached_projects = []

            try:
                # ── CHECK FOR MODE SWITCHES ──
                t_lower = user_text.lower()

                # ── PLANNING MODE: answering clarifying questions ──
                if planner.is_planning:
                    # Check for bypass
                    if any(p in t_lower for p in BYPASS_PHRASES):
                        plan = planner.active_plan
                        if plan:
                            plan.skipped = True
                            for q in plan.pending_questions[plan.current_question_index :]:
                                if q.get("default") is not None and q["key"] not in plan.answers:
                                    plan.answers[q["key"]] = q["default"]
                        prompt = await planner.build_prompt()
                        name = _generate_project_name(prompt)
                        path = str(Path.home() / "Desktop" / name)
                        os.makedirs(path, exist_ok=True)
                        Path(path, "CLAUDE.md").write_text(prompt)
                        did = dispatch_registry.register(name, path, prompt[:200])
                        asyncio.create_task(
                            _execute_prompt_project(
                                name,
                                prompt,
                                work_session,
                                ws,
                                dispatch_id=did,
                                history=history,
                                voice_state=voice_state,
                            )
                        )
                        planner.reset()
                        response_text = "Building it now, sir."
                    elif (
                        planner.active_plan
                        and planner.active_plan.confirmed is False
                        and planner.active_plan.current_question_index >= len(planner.active_plan.pending_questions)
                    ):
                        # Confirmation phase
                        result = await planner.handle_confirmation(user_text)
                        if result["confirmed"]:
                            prompt = await planner.build_prompt()
                            name = _generate_project_name(prompt)
                            path = str(Path.home() / "Desktop" / name)
                            os.makedirs(path, exist_ok=True)
                            Path(path, "CLAUDE.md").write_text(prompt)
                            did = dispatch_registry.register(name, path, prompt[:200])
                            asyncio.create_task(
                                _execute_prompt_project(
                                    name,
                                    prompt,
                                    work_session,
                                    ws,
                                    dispatch_id=did,
                                    history=history,
                                    voice_state=voice_state,
                                )
                            )
                            planner.reset()
                            response_text = "On it, sir."
                        elif result["cancelled"]:
                            planner.reset()
                            response_text = "Cancelled, sir."
                        else:
                            response_text = result.get("modification_question", "How shall I adjust the plan, sir?")
                    else:
                        result = await planner.process_answer(user_text, cached_projects)
                        if result["plan_complete"]:
                            response_text = result.get("confirmation_summary", "Ready to build. Shall I proceed, sir?")
                        else:
                            response_text = result.get("next_question", "What else, sir?")

                elif any(
                    w in t_lower
                    for w in ["quit work mode", "exit work mode", "go back to chat", "regular mode", "stop working"]
                ):
                    if work_session.active:
                        await work_session.stop()
                        response_text = "Back to conversation mode, sir."
                    else:
                        response_text = "Already in conversation mode, sir."

                # ── WORK MODE: speech → claude -p → Haiku summary → JARVIS voice ──
                elif work_session.active:
                    if is_casual_question(user_text):
                        # Quick chat — bypass claude -p, use Haiku
                        response_text = await generate_response(
                            user_text,
                            anthropic_client,
                            task_manager,
                            cached_projects,
                            history,
                            last_response=last_jarvis_response,
                            session_summary=session_summary,
                        )
                    else:
                        # Send to claude -p (full power)
                        await ws.send_json({"type": "status", "state": "working"})
                        log.info(f"Work mode → claude -p: {user_text[:80]}")

                        full_response = await work_session.send(user_text)

                        # Detect if Claude Code is stalling (asking questions instead of building)
                        if full_response and anthropic_client:
                            stall_words = [
                                "which option",
                                "would you prefer",
                                "would you like me to",
                                "before I proceed",
                                "before proceeding",
                                "should I",
                                "do you want me to",
                                "let me know",
                                "please confirm",
                                "which approach",
                                "what would you",
                            ]
                            is_stalling = any(w in full_response.lower() for w in stall_words)
                            if is_stalling and work_session._message_count >= 2:
                                # Claude Code keeps asking — push it to build
                                log.info("Claude Code stalling — pushing to build")
                                push_response = await work_session.send(
                                    "Stop asking questions. Use your best judgment and start building now. "
                                    "Write the actual code files. Go with the simplest reasonable approach."
                                )
                                if push_response:
                                    full_response = push_response

                        # Auto-open any localhost URLs Claude Code mentions
                        import re as _re

                        localhost_match = _re.search(r"https?://localhost:\d+", full_response or "")
                        if localhost_match:
                            asyncio.create_task(_execute_browse(localhost_match.group(0)))
                            log.info(f"Auto-opening {localhost_match.group(0)}")

                        # Always summarize work mode responses via Haiku
                        if full_response and anthropic_client:
                            try:
                                summary = await anthropic_client.messages.create(
                                    model="claude-haiku-4-5-20251001",
                                    max_tokens=100,
                                    system=(
                                        f"You are JARVIS reporting to the user ({USER_NAME}). Summarize what happened in 1-2 sentences. "
                                        "Speak in first person — 'I built', 'I found', 'I set up'. "
                                        "You are talking TO THE USER, not to a coding tool. "
                                        "NEVER give instructions like 'go ahead and build' or 'set up the frontend' — those are NOT for the user. "
                                        "NEVER say 'Claude Code'. NEVER output [ACTION:...] tags. "
                                        "NEVER read out URLs. No markdown. British precision."
                                    ),
                                    messages=[
                                        {"role": "user", "content": f"Claude Code said:\n{full_response[:2000]}"}
                                    ],
                                )
                                response_text = summary.content[0].text
                            except Exception:
                                response_text = full_response[:200]
                        else:
                            response_text = full_response

                # ── CHAT MODE: fast keyword detection + Haiku ──
                else:
                    action = detect_action_fast(user_text)

                    if action:
                        if action["action"] == "open_terminal":
                            response_text = await handle_open_terminal()
                        elif action["action"] == "show_recent":
                            response_text = await handle_show_recent()
                        elif action["action"] == "describe_screen":
                            response_text = "Taking a look now, sir."
                            asyncio.create_task(
                                _lookup_and_report(
                                    "screen", _do_screen_lookup, ws, history=history, voice_state=voice_state
                                )
                            )
                        elif action["action"] == "check_calendar":
                            response_text = "Checking your calendar now, sir."
                            asyncio.create_task(
                                _lookup_and_report(
                                    "calendar", _do_calendar_lookup, ws, history=history, voice_state=voice_state
                                )
                            )
                        elif action["action"] == "check_mail":
                            response_text = "Checking your inbox now, sir."
                            asyncio.create_task(
                                _lookup_and_report(
                                    "mail", _do_mail_lookup, ws, history=history, voice_state=voice_state
                                )
                            )
                        elif action["action"] == "check_dispatch":
                            recent = dispatch_registry.get_most_recent()
                            if not recent:
                                response_text = "No recent builds on record, sir."
                            else:
                                name = recent["project_name"]
                                status = recent["status"]
                                if status == "building" or status == "pending":
                                    elapsed = int(time.time() - recent["updated_at"])
                                    response_text = f"Still working on {name}, sir. Been at it for {elapsed} seconds."
                                elif status == "completed":
                                    response_text = recent.get("summary") or f"{name} is complete, sir."
                                elif status in ("failed", "timeout"):
                                    response_text = f"{name} ran into problems, sir."
                                else:
                                    response_text = f"{name} is {status}, sir."
                        elif action["action"] == "check_sessions":
                            response_text = session_manager.format_for_voice()
                        elif action["action"] == "check_tasks":
                            # Get both not-started and in-progress from MC
                            pending = await mc_client.list_tasks(kanban="not-started", limit=20)
                            active = await mc_client.list_tasks(kanban="in-progress", limit=20)
                            mc_tasks = active + pending
                            response_text = format_mc_tasks_for_voice(mc_tasks)
                        elif action["action"] == "check_inbox":
                            messages = await mc_client.list_inbox(agent="me", status="unread", limit=10)
                            response_text = format_mc_inbox_for_voice(messages)
                        elif action["action"] == "check_decisions":
                            decisions = await mc_client.list_decisions(status="pending")
                            response_text = format_mc_decisions_for_voice(decisions)
                        elif action["action"] == "check_usage":
                            response_text = get_usage_summary()
                        else:
                            response_text = "Understood, sir."
                    else:
                        if not anthropic_client:
                            response_text = "API key not configured."
                        else:
                            response_text = await generate_response(
                                user_text,
                                anthropic_client,
                                task_manager,
                                cached_projects,
                                history,
                                last_response=last_jarvis_response,
                                session_summary=session_summary,
                            )

                            # Check for action tags embedded in LLM response
                            clean_response, embedded_action = extract_action(response_text)
                            if embedded_action:
                                # Validate action wasn't injected from untrusted context
                                action_tag = f"[ACTION:{embedded_action['action'].upper()}]"
                                untrusted = [
                                    _ctx_cache.get("calendar", ""),
                                    _ctx_cache.get("mail", ""),
                                    _ctx_cache.get("screen", ""),
                                ]
                                if any(action_tag in ctx for ctx in untrusted if ctx):
                                    log.warning(
                                        f"Blocked potentially injected action from untrusted context: {action_tag}"
                                    )
                                    embedded_action = None
                            if embedded_action:
                                log.info(f"LLM embedded action: {embedded_action}")
                                response_text = clean_response
                                # Ensure there's always something to speak
                                if not response_text.strip():
                                    action_type = embedded_action["action"]
                                    if action_type == "prompt_project":
                                        proj = embedded_action["target"].split("|||")[0].strip()
                                        response_text = f"Connecting to {proj} now, sir."
                                    elif action_type == "build":
                                        response_text = "On it, sir."
                                    elif action_type == "research":
                                        response_text = "Looking into that now, sir."
                                    else:
                                        response_text = "Right away, sir."

                                if embedded_action["action"] == "build":
                                    # Dispatch build to Mission Control — daemon handles execution
                                    target = embedded_action["target"]
                                    mc_task = await mc_client.create_task(
                                        title=_generate_project_name(target),
                                        description=target,
                                        importance="important",
                                        urgency="urgent",
                                        assigned_to="developer",
                                    )
                                    if mc_task:
                                        log.info(f"MC build task created: {mc_task['id']} — {mc_task['title']}")
                                    else:
                                        # Fallback: old direct dispatch if MC is offline
                                        log.warning("MC offline — falling back to direct dispatch")
                                        name = _generate_project_name(target)
                                        path = str(Path.home() / "Desktop" / name)
                                        os.makedirs(path, exist_ok=True)
                                        Path(path, "CLAUDE.md").write_text(
                                            f"# Task\n\n{target}\n\nBuild this completely.\n"
                                        )
                                        did = dispatch_registry.register(name, path, target)
                                        asyncio.create_task(
                                            _execute_prompt_project(
                                                name,
                                                target,
                                                work_session,
                                                ws,
                                                dispatch_id=did,
                                                history=history,
                                                voice_state=voice_state,
                                            )
                                        )
                                elif embedded_action["action"] == "browse":
                                    asyncio.create_task(_execute_browse(embedded_action["target"]))
                                elif embedded_action["action"] == "research":
                                    # Dispatch research to Mission Control
                                    target = embedded_action["target"]
                                    mc_task = await mc_client.create_task(
                                        title=f"Research: {target[:80]}",
                                        description=target,
                                        importance="important",
                                        urgency="not-urgent",
                                        assigned_to="researcher",
                                    )
                                    if mc_task:
                                        log.info(f"MC research task created: {mc_task['id']}")
                                    else:
                                        # Fallback: old direct dispatch if MC is offline
                                        log.warning("MC offline — falling back to direct research")
                                        name = _generate_project_name(target)
                                        path = str(Path.home() / "Desktop" / name)
                                        os.makedirs(path, exist_ok=True)
                                        await work_session.start(path)
                                        asyncio.create_task(self_work_and_notify(work_session, target, ws))
                                elif embedded_action["action"] == "open_terminal":
                                    asyncio.create_task(_execute_open_terminal())
                                elif embedded_action["action"] == "prompt_project":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        proj_name, _, prompt = target.partition("|||")
                                        proj_name = proj_name.strip()
                                        prompt = prompt.strip()
                                        # Always dispatch fresh — caching caused stale/repeated responses
                                        asyncio.create_task(
                                            _execute_prompt_project(
                                                proj_name,
                                                prompt,
                                                work_session,
                                                ws,
                                                history=history,
                                                voice_state=voice_state,
                                            )
                                        )
                                    else:
                                        log.warning(f"PROMPT_PROJECT missing ||| delimiter: {target}")
                                elif embedded_action["action"] == "add_task":
                                    target = embedded_action["target"]
                                    parts = target.split("|||")
                                    if len(parts) >= 2:
                                        priority = parts[0].strip() or "medium"
                                        title = parts[1].strip()
                                        desc = parts[2].strip() if len(parts) > 2 else ""
                                        # Map JARVIS priority → Eisenhower matrix
                                        importance = "important" if priority in ("high", "medium") else "not-important"
                                        urgency = "urgent" if priority == "high" else "not-urgent"
                                        await mc_client.create_task(
                                            title=title,
                                            description=desc,
                                            importance=importance,
                                            urgency=urgency,
                                            assigned_to="me",
                                        )
                                        log.info(f"MC task created: {title}")
                                elif embedded_action["action"] == "add_note":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        topic, _, content = target.partition("|||")
                                        create_note(content=content.strip(), topic=topic.strip())
                                    else:
                                        create_note(content=target)
                                    log.info("Note created")
                                elif embedded_action["action"] == "complete_task":
                                    task_id = embedded_action["target"].strip()
                                    if task_id:
                                        await mc_client.complete_task(task_id)
                                        log.info(f"MC task {task_id} completed")
                                elif embedded_action["action"] == "remember":
                                    remember(embedded_action["target"].strip(), mem_type="fact", importance=7)
                                    log.info(f"Memory stored: {embedded_action['target'][:60]}")
                                elif embedded_action["action"] == "create_note":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        title, _, body = target.partition("|||")
                                        asyncio.create_task(create_apple_note(title.strip(), body.strip()))
                                        log.info(f"Apple Note created: {title.strip()}")
                                    else:
                                        asyncio.create_task(create_apple_note("JARVIS Note", target))
                                elif embedded_action["action"] == "screen":
                                    asyncio.create_task(
                                        _lookup_and_report(
                                            "screen", _do_screen_lookup, ws, history=history, voice_state=voice_state
                                        )
                                    )
                                elif embedded_action["action"] == "read_note":
                                    # Read note in background and report back
                                    async def _read_and_report(search_term, _ws):
                                        note = await read_note(search_term)
                                        if note:
                                            msg = f"Sir, your note '{note['title']}' says: {note['body'][:200]}"
                                        else:
                                            msg = f"Couldn't find a note matching '{search_term}', sir."
                                        audio = await synthesize_speech(strip_markdown_for_tts(msg))
                                        if audio and _ws:
                                            try:
                                                await _ws.send_json({"type": "status", "state": "speaking"})
                                                await _ws.send_json(
                                                    {
                                                        "type": "audio",
                                                        "data": base64.b64encode(audio).decode(),
                                                        "text": msg,
                                                    }
                                                )
                                            except Exception:
                                                pass

                                    asyncio.create_task(_read_and_report(embedded_action["target"].strip(), ws))
                                elif embedded_action["action"] == "set_timer":
                                    # Parse: "5 minutes ||| check on the build" or just "5 minutes"
                                    target = embedded_action["target"]
                                    parts = target.split("|||")
                                    time_str = parts[0].strip()
                                    reminder_msg = parts[1].strip() if len(parts) > 1 else "Your timer is up, sir."

                                    # Parse seconds from time string
                                    import re as _timer_re

                                    seconds = 0
                                    hrs = _timer_re.search(r"(\d+)\s*h", time_str)
                                    mins = _timer_re.search(r"(\d+)\s*m", time_str)
                                    secs = _timer_re.search(r"(\d+)\s*s", time_str)
                                    if hrs:
                                        seconds += int(hrs.group(1)) * 3600
                                    if mins:
                                        seconds += int(mins.group(1)) * 60
                                    if secs:
                                        seconds += int(secs.group(1))
                                    if not seconds:
                                        # Try bare number as minutes
                                        bare = _timer_re.search(r"(\d+)", time_str)
                                        if bare:
                                            seconds = int(bare.group(1)) * 60

                                    if seconds > 0:

                                        async def _timer_fire(_seconds, _msg, _ws):
                                            await asyncio.sleep(_seconds)
                                            audio = await synthesize_speech(strip_markdown_for_tts(_msg))
                                            if audio and _ws:
                                                try:
                                                    await _ws.send_json({"type": "status", "state": "speaking"})
                                                    await _ws.send_json(
                                                        {
                                                            "type": "audio",
                                                            "data": base64.b64encode(audio).decode(),
                                                            "text": _msg,
                                                        }
                                                    )
                                                except Exception:
                                                    pass

                                        asyncio.create_task(_timer_fire(seconds, reminder_msg, ws))
                                        log.info(f"Timer set: {seconds}s — {reminder_msg}")

                # Update history
                history.append({"role": "user", "content": user_text})
                history.append({"role": "assistant", "content": response_text})

                # Three-tier memory: also track in session buffer
                session_buffer.append({"role": "user", "content": user_text})
                session_buffer.append({"role": "assistant", "content": response_text})

                # Check if rolling summary needs updating
                messages_since_last_summary += 1
                if messages_since_last_summary >= 5 and len(history) > 20 and not summary_update_pending:
                    summary_update_pending = True
                    messages_since_last_summary = 0
                    # Get messages that are about to be rotated out
                    rotated = history[:-20] if len(history) > 20 else []
                    if rotated and anthropic_client:

                        async def _do_summary(_rotated=rotated):
                            nonlocal session_summary, summary_update_pending
                            session_summary = await _update_session_summary(session_summary, _rotated, anthropic_client)
                            summary_update_pending = False

                        asyncio.create_task(_do_summary())
                    else:
                        summary_update_pending = False

                # Extract memories in background (doesn't block response)
                if anthropic_client and len(user_text) > 15:
                    asyncio.create_task(extract_memories(user_text, response_text, anthropic_client))

                # TTS
                tts = strip_markdown_for_tts(response_text)
                await ws.send_json({"type": "status", "state": "speaking"})
                audio = await synthesize_speech(tts)
                if audio:
                    await ws.send_json(
                        {"type": "audio", "data": base64.b64encode(audio).decode(), "text": response_text}
                    )
                else:
                    await ws.send_json({"type": "text", "text": response_text})
                    await ws.send_json({"type": "status", "state": "idle"})
                log.info(f"JARVIS: {response_text}")
                last_jarvis_response = response_text

            except Exception as e:
                log.error(f"Error: {e}", exc_info=True)
                try:
                    fallback = "Something went wrong, sir."
                    audio = await synthesize_speech(fallback)
                    if audio:
                        await ws.send_json(
                            {"type": "audio", "data": base64.b64encode(audio).decode(), "text": fallback}
                        )
                    else:
                        await ws.send_json({"type": "audio", "data": "", "text": fallback})
                    # Let client's audioPlayer.onFinished handle idle transition
                except Exception:
                    pass

    except WebSocketDisconnect:
        log.info("Voice WebSocket disconnected")
    except Exception as e:
        log.error(f"WebSocket error: {e}", exc_info=True)
    finally:
        task_manager.unregister_websocket(ws)


# ---------------------------------------------------------------------------
# Settings / Configuration endpoints — see api_settings.py
# ---------------------------------------------------------------------------

app.include_router(build_settings_router(require_auth, FISH_VOICE_ID))


# ---------------------------------------------------------------------------
# Control endpoints (restart, fix-self)
# ---------------------------------------------------------------------------


@app.post("/api/restart", dependencies=[Depends(require_auth)])
async def api_restart():
    """Restart the JARVIS server."""
    if not ALLOW_REMOTE_CONTROL:
        return JSONResponse(
            status_code=403, content={"error": "Remote control disabled. Set ALLOW_REMOTE_CONTROL=true in .env"}
        )
    log.info("Restart requested — shutting down in 2 seconds")

    async def _restart():
        await asyncio.sleep(2)
        cmd = [sys.executable, __file__, "--port", "8340", "--host", "127.0.0.1"]
        os.execv(sys.executable, cmd)

    asyncio.create_task(_restart())
    return {"status": "restarting"}


@app.post("/api/fix-self", dependencies=[Depends(require_auth)])
async def api_fix_self():
    """Enter work mode in the JARVIS repo — JARVIS can now fix himself."""
    if not ALLOW_REMOTE_CONTROL:
        return JSONResponse(
            status_code=403, content={"error": "Remote control disabled. Set ALLOW_REMOTE_CONTROL=true in .env"}
        )
    jarvis_dir = str(Path(__file__).parent)

    # Launch in tmux session for monitoring, open Terminal attached to it
    from tmux_sessions import TMUX_AVAILABLE

    if TMUX_AVAILABLE:
        cmd = f"claude{DANGEROUS_FLAG}"
        tmux = await session_manager.create_session("jarvis-self", jarvis_dir, command=cmd, mode="interactive")
        if tmux:
            await session_manager.attach_in_terminal(tmux.name)
            log.info("Work mode: JARVIS repo opened for self-improvement (tmux)")
            return {"status": "work_mode_active", "path": jarvis_dir}

    # Fallback: AppleScript
    script = (
        'tell application "Terminal"\n'
        "    activate\n"
        f'    do script "cd {escape_shell_in_applescript(jarvis_dir)} && claude{DANGEROUS_FLAG}"\n'
        "end tell"
    )
    await asyncio.create_subprocess_exec(
        "osascript",
        "-e",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    log.info("Work mode: JARVIS repo opened for self-improvement")
    return {"status": "work_mode_active", "path": jarvis_dir}


# ---------------------------------------------------------------------------
# Static file serving (frontend)
# ---------------------------------------------------------------------------

from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"

if FRONTEND_DIST.exists():

    @app.get("/")
    async def serve_index():
        return FileResponse(str(FRONTEND_DIST / "index.html"))

    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="JARVIS Server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (use 0.0.0.0 for LAN access)")
    parser.add_argument("--port", type=int, default=8340, help="Bind port")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on changes")
    parser.add_argument("--ssl", action="store_true", help="Enable HTTPS with key.pem/cert.pem")
    args = parser.parse_args()

    # Auto-detect SSL certs
    cert_file = Path(__file__).parent / "cert.pem"
    key_file = Path(__file__).parent / "key.pem"
    use_ssl = args.ssl or (cert_file.exists() and key_file.exists())

    proto = "https" if use_ssl else "http"
    ws_proto = "wss" if use_ssl else "ws"

    print()
    print("  J.A.R.V.I.S. Server v0.1.0")
    print(f"  WebSocket: {ws_proto}://{args.host}:{args.port}/ws/voice")
    print(f"  REST API:  {proto}://{args.host}:{args.port}/api/")
    print(f"  Tasks:     {proto}://{args.host}:{args.port}/api/tasks")
    print()

    ssl_kwargs = {}
    if use_ssl:
        ssl_kwargs["ssl_keyfile"] = str(key_file)
        ssl_kwargs["ssl_certfile"] = str(cert_file)

    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        **ssl_kwargs,
    )
