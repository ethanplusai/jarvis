"""Readiness and explicit repairs; HTTP liveness remains separate."""
import asyncio
from dataclasses import asdict
import time

from fastapi import APIRouter, HTTPException

import preflight
import run_store
import diagnostics_state


from platform_capabilities import capabilities


def router(runtime, repair):
    api = APIRouter()
    lock = asyncio.Lock()

    async def refresh():
        async with lock:
            diagnostics_state.record_checks(await preflight.run_checks())

    @api.get("/api/capabilities")
    async def get_capabilities():
        return capabilities()

    @api.get("/api/readiness")
    async def readiness():
        state = runtime()
        checks = diagnostics_state.checks
        observed = diagnostics_state.checked_at
        fresh = observed is not None and time.time() - observed <= 300
        login_check = next((c for c in checks if c.name == "claude_login"), None)
        login = ("ready" if login_check.ok else "unavailable") if fresh and login_check else "unchecked"
        try:
            await asyncio.to_thread(run_store.stats)
            database = "ready"
        except Exception:
            database = "unavailable"
        ready = (state.get("brain_ready", False) and database == "ready" and login == "ready"
                 and not state.get("shutting_down", False))
        tts = diagnostics_state.tts_status if state.get("tts_configured") else "not_configured"
        if diagnostics_state.tts_checked_at and time.time() - diagnostics_state.tts_checked_at > 300:
            tts = "unchecked" if state.get("tts_configured") else "not_configured"
        return {"ready": ready, "database": database, **state,
                "text_ready": ready, "voice_ready": ready and tts == "ready",
                "login": login, "tts": tts, "checks_stale": not fresh,
                "tts_checked_at": diagnostics_state.tts_checked_at,
                "capabilities": capabilities(), "checked_at": observed,
                "checks": [asdict(c) for c in checks]}

    @api.post("/api/diagnostics/check")
    async def check():
        await refresh()
        return await readiness()

    @api.post("/api/diagnostics/repair/{action}")
    async def fix(action: str):
        if action not in {"restart-brain", "open-login"}:
            raise HTTPException(400, "Unknown repair action")
        await repair(action)
        await refresh()
        return await readiness()

    return api
