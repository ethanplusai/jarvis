from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import diagnostics_api
import run_store


def test_readiness_reports_storage_failure_and_rejects_unknown_repairs(monkeypatch):
    import diagnostics_state
    diagnostics_state.record_checks([diagnostics_api.preflight.Check("claude_login", "ok", "Logged in")])
    state = {"brain_ready": True}
    repaired = []
    async def repair(action):
        repaired.append(action)
    async def checks():
        return []
    monkeypatch.setattr(diagnostics_api.preflight, "run_checks", checks)
    run_store.init_db()
    app = FastAPI()
    app.include_router(diagnostics_api.router(lambda: state, repair))
    with TestClient(app) as client:
        assert client.get("/api/readiness").json()["ready"] is True
        state["brain_ready"] = False
        assert client.get("/api/readiness").json()["ready"] is False
        assert client.post("/api/diagnostics/repair/arbitrary-command").status_code == 400
        assert repaired == []
        assert client.post("/api/diagnostics/repair/restart-brain").status_code == 200
        assert repaired == ["restart-brain"]
        def broken():
            raise OSError("private error")
        monkeypatch.setattr(run_store, "stats", broken)
        result = client.get("/api/readiness").json()
        assert result["database"] == "unavailable"
        assert "private error" not in str(result)


def test_readiness_does_not_invent_login_or_voice_readiness(monkeypatch):
    import diagnostics_state as state
    monkeypatch.setattr(state, "checks", [])
    monkeypatch.setattr(state, "checked_at", None)
    monkeypatch.setattr(state, "tts_status", "unchecked")
    monkeypatch.setattr(state, "tts_checked_at", None)
    run_store.init_db()
    app = FastAPI()
    app.include_router(diagnostics_api.router(lambda: {"brain_ready": True, "tts_configured": True}, None))
    with TestClient(app) as client:
        result = client.get("/api/readiness").json()
        assert result["login"] == "unchecked" and not result["ready"]
        assert result["tts"] == "unchecked" and not result["voice_ready"]
        state.record_checks([diagnostics_api.preflight.Check("claude_login", "ok", "Logged in")])
        state.record_tts(True)
        assert client.get("/api/readiness").json()["voice_ready"] is True
        state.record_tts(False)
        result = client.get("/api/readiness").json()
        assert result["text_ready"] is True and result["voice_ready"] is False
        state.checked_at -= 301
        assert client.get("/api/readiness").json()["login"] == "unchecked"


@pytest.mark.asyncio
async def test_cancelled_preflight_reaps_its_child(monkeypatch):
    import asyncio
    import preflight
    started = asyncio.Event()
    class Child:
        returncode = None
        killed = False
        reaped = False
        async def communicate(self):
            if self.killed:
                self.reaped = True
                return b"", b""
            started.set()
            await asyncio.Future()
        def kill(self):
            self.killed = True
    child = Child()
    async def spawn(*args, **kwargs):
        return child
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(preflight._run_subprocess("fake", timeout=60))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.killed and child.reaped
