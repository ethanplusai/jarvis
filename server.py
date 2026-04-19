"""
JARVIS Server — Voice AI + Development Orchestration

Handles:
1. WebSocket voice interface (browser audio <-> LLM <-> TTS)
2. Claude Code task manager (spawn/manage claude -p subprocesses)
3. Project awareness (scan Desktop for git repos)
4. REST API for task management
"""

import asyncio
import json
import logging
import os
import secrets
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
from pathlib import Path

import anthropic
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from api import build_control_router, build_core_router, build_settings_router, build_voice_router
from context_cache import start_context_refresh
from dispatch_registry import DispatchRegistry
from feedback import ABTester, SuccessTracker, UsageLearner
from formatting import (
    apply_speech_corrections,  # noqa: F401 — re-exported for tests
    extract_action,  # noqa: F401 — re-exported for tests
    format_projects_for_prompt,  # noqa: F401 — re-exported for tests
    strip_markdown_for_tts,  # noqa: F401 — re-exported for tests
)
from llm import (
    generate_response as _llm_generate_response,
)
from mc_client import mc_client
from mc_inbox import watch_inbox
from memory import (
    extract_memories,
)
from planner import TaskPlanner
from projects import scan_projects
from projects import scan_projects_sync as _scan_projects_sync
from qa import QAAgent
from suggestions import suggest_followup
from task_manager import ClaudeTaskManager
from usage import (
    append_usage_entry as _append_usage_entry,  # noqa: F401
)
from usage import (
    cost_from_tokens as _cost_from_tokens,  # noqa: F401
)
from voice import (
    SessionMemory,
    detect_action_fast,  # noqa: F401 — re-exported for tests
    do_calendar_lookup,
    do_mail_lookup,
    do_screen_lookup,
    execute_prompt_project,
    execute_research,
    get_lookup_status,
    handle_chat_message,
    handle_planning_message,
    handle_work_mode_message,
    maybe_greet,
    speak,
    speak_fallback,
)
from voice import (
    lookup_and_report as _lookup_and_report,
)
from voice import (
    self_work_and_notify as _self_work_and_notify,
)
from work_mode import WorkSession, is_casual_question

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("jarvis")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
FISH_VOICE_ID = os.getenv("FISH_VOICE_ID", "612b878b113047d9a770c069c8b4fdfe")  # JARVIS (MCU)
USER_NAME = os.getenv("USER_NAME", "sir")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


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
# Thin adapters that bind module-global runtime state into pure helpers
# imported from voice/. The voice package is self-contained (no server.py
# import), so server.py hands it the anthropic_client / dispatch_registry /
# cached_projects references through these wrappers.
# ---------------------------------------------------------------------------


async def _execute_research(target: str, ws=None):
    await execute_research(target, ws)


async def _execute_prompt_project(
    project_name: str,
    prompt: str,
    work_session: WorkSession,
    ws,
    dispatch_id: int | None = None,
    history: list[dict] | None = None,
    voice_state: dict | None = None,
):
    await execute_prompt_project(
        project_name,
        prompt,
        work_session,
        ws,
        anthropic_client=anthropic_client,
        dispatch_registry=dispatch_registry,
        cached_projects=cached_projects,
        dispatch_id=dispatch_id,
        history=history,
        voice_state=voice_state,
    )


async def self_work_and_notify(session: WorkSession, prompt: str, ws):
    await _self_work_and_notify(session, prompt, ws, anthropic_client=anthropic_client)


_AUTH_TOKEN: str = ""


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
    start_context_refresh(_ctx_cache)

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
    inbox_task = asyncio.create_task(watch_inbox(task_manager._notify))

    log.info("JARVIS server starting")

    yield

    inbox_task.cancel()


app = FastAPI(title="JARVIS Server", version="0.1.0", lifespan=lifespan)

# Rate limiting — defense in depth. Single-user localhost means 60/min is generous.
# If you expose JARVIS to a network, reduce this or add per-route limits.
from slowapi import Limiter, _rate_limit_exceeded_handler  # noqa: E402
from slowapi.errors import RateLimitExceeded  # noqa: E402
from slowapi.middleware import SlowAPIMiddleware  # noqa: E402
from slowapi.util import get_remote_address  # noqa: E402

limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)  # applies default_limits to every route

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


# -- REST Endpoints — see api_core.py --------------------------------------


async def _refresh_projects() -> list[dict]:
    global cached_projects
    cached_projects = await scan_projects()
    return cached_projects


app.include_router(build_core_router(require_auth, task_manager, dispatch_registry, _refresh_projects))


# -- Fast Action Detection (no LLM call) -----------------------------------


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

    # Three-tier conversation memory — see session_memory.SessionMemory
    memory = SessionMemory()

    log.info("Voice WebSocket connected")

    try:
        # ── Greeting — always start in conversation mode ──
        maybe_greet(ws, history)

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
                await speak(ws, "Work mode active in my own repo, sir. Tell me what needs fixing.")
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
                    response_text = await handle_planning_message(
                        user_text,
                        t_lower,
                        planner=planner,
                        work_session=work_session,
                        ws=ws,
                        history=history,
                        voice_state=voice_state,
                        cached_projects=cached_projects,
                        dispatch_registry=dispatch_registry,
                        execute_prompt_project=_execute_prompt_project,
                    )

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

                    async def _casual(
                        _ut=user_text,
                        _ac=anthropic_client,
                        _cp=cached_projects,
                        _lr=last_jarvis_response,
                        _ss=memory.summary,
                    ):
                        return await generate_response(
                            _ut,
                            _ac,
                            task_manager,
                            _cp,
                            history,
                            last_response=_lr,
                            session_summary=_ss,
                        )

                    response_text = await handle_work_mode_message(
                        user_text,
                        ws=ws,
                        work_session=work_session,
                        anthropic_client=anthropic_client,
                        user_name=USER_NAME,
                        generate_casual_response=_casual,
                        is_casual=is_casual_question(user_text),
                    )

                # ── CHAT MODE: fast keyword detection + Haiku ──
                else:

                    async def _chat_generate(
                        text,
                        _ac=anthropic_client,
                        _cp=cached_projects,
                        **kwargs,
                    ):
                        return await generate_response(text, _ac, task_manager, _cp, history, **kwargs)

                    response_text = await handle_chat_message(
                        user_text,
                        ws=ws,
                        work_session=work_session,
                        history=history,
                        voice_state=voice_state,
                        ctx_cache=_ctx_cache,
                        anthropic_client=anthropic_client,
                        dispatch_registry=dispatch_registry,
                        last_jarvis_response=last_jarvis_response,
                        session_summary=memory.summary,
                        generate_response=_chat_generate,
                        execute_prompt_project=_execute_prompt_project,
                        self_work_and_notify=self_work_and_notify,
                        lookup_and_report=_lookup_and_report,
                        do_screen_lookup=_do_screen_lookup,
                        do_calendar_lookup=_do_calendar_lookup,
                        do_mail_lookup=_do_mail_lookup,
                    )

                # Update history + three-tier memory, schedule rolling summary refresh
                memory.record(user_text, response_text, history)
                memory.maybe_refresh(history, anthropic_client)

                # Extract memories in background (doesn't block response)
                if anthropic_client and len(user_text) > 15:
                    asyncio.create_task(extract_memories(user_text, response_text, anthropic_client))

                await speak(ws, response_text)
                log.info(f"JARVIS: {response_text}")
                last_jarvis_response = response_text

            except Exception as e:
                log.error(f"Error: {e}", exc_info=True)
                await speak_fallback(ws, "Something went wrong, sir.")

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


app.include_router(build_control_router(require_auth, __file__))


# ---------------------------------------------------------------------------
# Voice ID endpoints (enroll, verify, status) — see api/voice.py
# ---------------------------------------------------------------------------


app.include_router(build_voice_router(require_auth))


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
