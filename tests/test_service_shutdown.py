import asyncio
import pytest


@pytest.mark.asyncio
async def test_shared_shutdown_drains_background_before_brain_and_is_idempotent(monkeypatch):
    import server
    events = []
    started = asyncio.Event()
    class Executor:
        async def shutdown(self):
            events.append("runs")
    async def watcher():
        events.append("watcher")
    async def brain():
        events.append("brain")
    async def background():
        try:
            started.set()
            await asyncio.Future()
        finally:
            events.append("background")
    task = asyncio.create_task(background())
    await started.wait()
    monkeypatch.setattr(server, "run_executor_instance", Executor())
    monkeypatch.setattr(server, "stop_session_watcher", watcher)
    monkeypatch.setattr(server, "stop_brain_and_speech", brain)
    monkeypatch.setattr(server, "_background", {task})
    monkeypatch.setattr(server, "_bg_tasks", set())
    monkeypatch.setattr(server, "_services_stopped", False)
    monkeypatch.setattr(server, "_shutdown_lock", asyncio.Lock())
    monkeypatch.setattr(server.maintenance, "unregister_runtime", lambda: events.append("unlock"))
    await server.shutdown_services()
    await server.shutdown_services()
    assert events == ["runs", "watcher", "background", "brain", "unlock"]
    assert task.done()


@pytest.mark.parametrize("argv,original,expected", [
    (["server.py", "--port", "9001"], ["python", "server.py", "--port", "9001"],
     ["--port", "9001"]),
    (["C:\\venv\\Scripts\\uvicorn.exe", "server:app", "--port", "9002"], [],
     ["-m", "uvicorn", "server:app", "--port", "9002"]),
    (["__main__.py", "server:app", "--host", "::1"],
     ["python", "-m", "uvicorn", "server:app", "--host", "::1"],
     ["-m", "uvicorn", "server:app", "--host", "::1"]),
])
def test_restart_preserves_launcher_and_bind_arguments(monkeypatch, argv, original, expected):
    import server
    monkeypatch.setattr(server.sys, "argv", argv)
    monkeypatch.setattr(server.sys, "orig_argv", original)
    result = server._restart_arguments()
    assert result[0] == server.sys.executable
    if argv[0] == "server.py":
        assert result[1] == server.__file__
        assert result[2:] == expected
    else:
        assert result[1:] == expected


@pytest.mark.asyncio
async def test_brain_repair_keeps_history_when_utterance_numbers_restart(monkeypatch):
    import server
    import conversation_store
    conversation_store.init_db()
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    monkeypatch.setattr(server, "voice_clients", set())
    async def no_handover(**kwargs):
        return None
    monkeypatch.setattr(server, "_ask_for_journal", no_handover)
    for text in ("Before repair", "After repair"):
        await server.start_brain_and_speech()
        try:
            with pytest.raises(server.NoVoiceClient):
                await server._voice_emit({"type": "text", "utt": 1, "idx": 0, "text": text})
        finally:
            await server.stop_brain_and_speech()
    assert [m["text"] for m in conversation_store.list_messages()] == ["Before repair", "After repair"]
