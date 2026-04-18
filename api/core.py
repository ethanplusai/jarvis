"""
Core API — /api/health, /api/tts-test, /api/usage, /api/tasks/*,
/api/projects, /api/sessions, /api/memory, /api/dispatches.

server.py builds the router with its runtime singletons injected so
the endpoints have clean access to task_manager, dispatch_registry,
session_manager, and the cached_projects list.
"""

import base64
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from memory import get_important_memories, get_open_tasks
from models import TaskRequest
from tts import synthesize_speech
from usage import (
    SESSION_START,
    SESSION_TOKENS,
    cost_from_tokens,
    get_usage_for_period,
)
from work_mode import session_manager


def build_core_router(
    require_auth: Callable,
    task_manager: Any,
    dispatch_registry: Any,
    refresh_projects: Callable[[], Awaitable[list[dict]]],
) -> APIRouter:
    """Build the core REST router.

    refresh_projects should scan the filesystem and return the updated
    cached_projects list — called by /api/projects.
    """
    router = APIRouter()

    @router.get("/api/health")
    async def health():
        return {"status": "online", "name": "JARVIS", "version": "0.1.0"}

    @router.get("/api/tts-test", dependencies=[Depends(require_auth)])
    async def tts_test():
        """Generate a test audio clip for debugging."""
        audio = await synthesize_speech("Testing audio, sir.")
        if audio:
            return {"audio": base64.b64encode(audio).decode()}
        return {"audio": None, "error": "TTS failed"}

    @router.get("/api/usage", dependencies=[Depends(require_auth)])
    async def api_usage():
        uptime = int(time.time() - SESSION_START)
        today = get_usage_for_period(86400)
        week = get_usage_for_period(86400 * 7)
        month = get_usage_for_period(86400 * 30)
        all_time = get_usage_for_period(None)
        return {
            "session": {**SESSION_TOKENS, "uptime_seconds": uptime},
            "today": {**today, "cost_usd": round(cost_from_tokens(today["input_tokens"], today["output_tokens"]), 4)},
            "week": {**week, "cost_usd": round(cost_from_tokens(week["input_tokens"], week["output_tokens"]), 4)},
            "month": {**month, "cost_usd": round(cost_from_tokens(month["input_tokens"], month["output_tokens"]), 4)},
            "all_time": {
                **all_time,
                "cost_usd": round(cost_from_tokens(all_time["input_tokens"], all_time["output_tokens"]), 4),
            },
        }

    @router.get("/api/tasks", dependencies=[Depends(require_auth)])
    async def api_list_tasks():
        tasks = await task_manager.list_tasks()
        return {"tasks": [t.to_dict() for t in tasks]}

    @router.get("/api/tasks/{task_id}", dependencies=[Depends(require_auth)])
    async def api_get_task(task_id: str):
        task = await task_manager.get_status(task_id)
        if not task:
            return JSONResponse(status_code=404, content={"error": "Task not found"})
        return {"task": task.to_dict()}

    @router.post("/api/tasks", dependencies=[Depends(require_auth)])
    async def api_create_task(req: TaskRequest):
        try:
            task_id = await task_manager.spawn(req.prompt, req.working_dir)
            return {"task_id": task_id, "status": "spawned"}
        except RuntimeError as e:
            return JSONResponse(status_code=429, content={"error": str(e)})

    @router.delete("/api/tasks/{task_id}", dependencies=[Depends(require_auth)])
    async def api_cancel_task(task_id: str):
        cancelled = await task_manager.cancel(task_id)
        if not cancelled:
            return JSONResponse(status_code=404, content={"error": "Task not found or not cancellable"})
        return {"task_id": task_id, "status": "cancelled"}

    @router.get("/api/projects", dependencies=[Depends(require_auth)])
    async def api_list_projects():
        projects = await refresh_projects()
        return {"projects": projects}

    @router.get("/api/sessions", dependencies=[Depends(require_auth)])
    async def api_list_sessions():
        sessions = await session_manager.list_sessions()
        return {"sessions": sessions}

    @router.get("/api/memory", dependencies=[Depends(require_auth)])
    async def api_memory():
        memories = get_important_memories(limit=20)
        tasks = get_open_tasks()
        return {"memories": memories, "tasks": tasks}

    @router.get("/api/dispatches", dependencies=[Depends(require_auth)])
    async def api_dispatches():
        active = dispatch_registry.get_active()
        recent = dispatch_registry.get_recent(limit=10)
        return {"active": active, "recent": recent}

    return router
