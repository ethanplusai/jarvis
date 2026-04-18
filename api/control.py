"""
Control API — /api/restart and /api/fix-self endpoints.

Both gate on ALLOW_REMOTE_CONTROL. server.py attaches the router via
build_control_router(require_auth, server_file).
"""

import asyncio
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from sanitize import ALLOW_REMOTE_CONTROL, DANGEROUS_FLAG, escape_shell_in_applescript
from work_mode import session_manager

log = logging.getLogger("jarvis.api_control")


def build_control_router(require_auth: Callable, server_file: str) -> APIRouter:
    """Build the /api/{restart,fix-self} router.

    server_file is the path to the server.py entrypoint (used for os.execv).
    """
    router = APIRouter(dependencies=[Depends(require_auth)])

    @router.post("/api/restart")
    async def api_restart():
        if not ALLOW_REMOTE_CONTROL:
            return JSONResponse(
                status_code=403, content={"error": "Remote control disabled. Set ALLOW_REMOTE_CONTROL=true in .env"}
            )
        log.info("Restart requested — shutting down in 2 seconds")

        async def _restart():
            await asyncio.sleep(2)
            cmd = [sys.executable, server_file, "--port", "8340", "--host", "127.0.0.1"]
            os.execv(sys.executable, cmd)  # noqa: S606 — server_file is __file__

        asyncio.create_task(_restart())
        return {"status": "restarting"}

    @router.post("/api/fix-self")
    async def api_fix_self():
        """Enter work mode in the JARVIS repo — JARVIS can now fix himself."""
        if not ALLOW_REMOTE_CONTROL:
            return JSONResponse(
                status_code=403, content={"error": "Remote control disabled. Set ALLOW_REMOTE_CONTROL=true in .env"}
            )
        jarvis_dir = str(Path(server_file).parent)

        from tmux_sessions import TMUX_AVAILABLE

        if TMUX_AVAILABLE:
            cmd = f"claude{DANGEROUS_FLAG}"
            tmux = await session_manager.create_session("jarvis-self", jarvis_dir, command=cmd, mode="interactive")
            if tmux:
                await session_manager.attach_in_terminal(tmux.name)
                log.info("Work mode: JARVIS repo opened for self-improvement (tmux)")
                return {"status": "work_mode_active", "path": jarvis_dir}

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

    return router
