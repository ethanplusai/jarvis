import asyncio
import sys
import pytest
import procs
import run_store
from run_executor import RunExecutor


@pytest.mark.asyncio
async def test_shutdown_stops_owned_process_when_status_read_fails(tmp_path, monkeypatch):
    run_store.init_db()
    script = tmp_path / "sleep.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    executor = RunExecutor(run_store, claude_path=f'"{sys.executable}" "{script}"', grace_sec=.2)
    run_id = await executor.spawn("x", "p", str(tmp_path), "test")
    async def running():
        while run_id not in executor._procs:
            await asyncio.sleep(.01)
        return executor._procs[run_id].pid
    pid = await asyncio.wait_for(running(), 5)
    def unavailable(*args):
        raise OSError("database read unavailable")
    try:
        with monkeypatch.context() as fault:
            fault.setattr(run_store, "get_run", unavailable)
            await asyncio.wait_for(executor.shutdown(), 5)
        assert not procs.pid_alive(pid)
        assert run_store.get_run(run_id)["status"] in run_store.RunStatus.TERMINAL
    finally:
        await executor.shutdown()
