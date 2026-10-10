import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import threading

import pytest

import procs
import run_store
from run_executor import RunExecutor


def test_terminal_transition_has_exactly_one_winner(tmp_path):
    run_store.init_db()
    run_id = run_store.create_run("p", "p", str(tmp_path), "test")
    barrier = threading.Barrier(3)

    def finish(status):
        barrier.wait(timeout=5)
        return run_store.transition_run(run_id, status, error=status, ended_at=42)

    with ThreadPoolExecutor(3) as pool:
        rows = list(pool.map(finish, ["succeeded", "failed", "cancelled"]))
    winners = [r for r in rows if r]
    assert len(winners) == 1
    assert run_store.get_run(run_id) == winners[0]
    assert run_store.transition_run(run_id, "running", pid=999) is None
    assert run_store.get_run(run_id) == winners[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "timeout", "shutdown"])
async def test_owned_descendants_stop_with_run(tmp_path, action):
    run_store.init_db()
    marker = tmp_path / "descendant.json"
    grandchild_marker = tmp_path / "grandchild.json"
    child = tmp_path / "child.py"
    child.write_text(
        "import subprocess,sys,time,json,pathlib\n"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        f"pathlib.Path({str(grandchild_marker)!r}).write_text(json.dumps(p.pid))\n"
        "time.sleep(60)\n", encoding="utf-8")
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess,sys,time,json,pathlib\n"
        f"p=subprocess.Popen([sys.executable,{str(child)!r}])\n"
        f"marker=pathlib.Path({str(grandchild_marker)!r})\n"
        "while not marker.exists() or not marker.read_text(): time.sleep(.02)\n"
        f"pathlib.Path({str(marker)!r}).write_text(json.dumps([p.pid,json.loads(marker.read_text())]))\n"
        "time.sleep(60)\n", encoding="utf-8")
    executor = RunExecutor(run_store, claude_path=f'"{sys.executable}" "{parent}"',
                           default_timeout_sec=2 if action == "timeout" else 60,
                           grace_sec=1)
    run_id = await executor.spawn("test", "p", str(tmp_path), "test")
    async def ready():
        while not marker.exists():
            await asyncio.sleep(.02)
        return json.loads(marker.read_text(encoding="utf-8"))
    pids = await asyncio.wait_for(ready(), 10)
    try:
        if action == "cancel":
            assert await executor.cancel(run_id)
        elif action == "shutdown":
            await executor.shutdown()
            with pytest.raises(RuntimeError, match="shutting down"):
                await executor.spawn("again", "p", str(tmp_path), "test")
        row = await executor.wait_for(run_id, 10)
        assert row["status"] == ("timed_out" if action == "timeout" else "cancelled")
        for _ in range(100):
            if not any(procs.pid_alive(pid) for pid in pids):
                break
            await asyncio.sleep(.02)
        assert not any(procs.pid_alive(pid) for pid in pids), "owned descendant survived termination"
    finally:
        await executor.shutdown()


@pytest.mark.asyncio
async def test_shutdown_cancels_queued_without_starting_it(tmp_path):
    run_store.init_db()
    executor = RunExecutor(run_store, max_concurrent=0)
    run_id = await executor.spawn("test", "p", str(tmp_path), "test")
    await executor.shutdown()
    assert run_store.get_run(run_id)["status"] == "cancelled"
    assert executor.active_count() == 0
