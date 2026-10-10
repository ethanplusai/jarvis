"""Offline, isolated probes for the 2026-09-20 audit; prints observations.

Run: .venv/Scripts/python.exe scripts/audit_lifecycle.py
No Claude process, network, credentials, or live database is used.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def terminal_race(store, executor):
    run_id = store.create_run("audit", "audit", ".", "audit")
    original = store.get_run
    barrier = threading.Barrier(2)
    local = threading.local()

    def read(run_id):
        row = original(run_id)
        if not getattr(local, "read", False):
            local.read = True
            barrier.wait(timeout=5)
        return row

    with patch.object(store, "get_run", read), ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(executor._finish_write, run_id, status)
                   for status in ("succeeded", "cancelled")]
        rows = [f.result(timeout=10) for f in futures]
    return {"accepted_terminal_writers": sum(row is not None for row in rows),
            "expected": 1, "final_status": original(run_id)["status"]}


async def descendant(executor, root):
    import process_tree
    from procs import pid_alive
    marker = root / "child.pid"
    code = ("import subprocess,sys,time,pathlib; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)")
    parent = await process_tree.spawn(sys.executable, "-c", code, str(marker))
    child_pid = None
    try:
        for _ in range(100):
            if marker.exists() and marker.read_text().strip():
                child_pid = int(marker.read_text())
                break
            await asyncio.sleep(.05)
        if child_pid is None:
            raise RuntimeError("Probe child did not start")
        await executor._terminate(parent)
        return {"parent_stopped": parent.returncode is not None,
                "descendant_still_alive": pid_alive(child_pid),
                "expected_descendant_alive": False}
    finally:
        process_tree.release(parent)
        if parent.returncode is None:
            parent.kill()
            await parent.wait()
        if child_pid is not None and pid_alive(child_pid):
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(child_pid), "/F"],
                               capture_output=True, check=True)
            else:
                import signal
                os.kill(child_pid, signal.SIGKILL)


def main():
    with tempfile.TemporaryDirectory(prefix="jarvis-audit-") as temp:
        os.environ["JARVIS_DATA_DIR"] = temp
        import run_store
        from run_executor import RunExecutor
        run_store.init_db()
        executor = RunExecutor(run_store, grace_sec=1)
        print(json.dumps({"terminal_race": terminal_race(run_store, executor),
                          "cancellation": asyncio.run(descendant(executor, Path(temp)))},
                         indent=2))


if __name__ == "__main__":
    main()
