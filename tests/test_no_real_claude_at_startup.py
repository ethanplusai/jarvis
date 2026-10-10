"""Opening a TestClient does not run the real `claude`.

Every `with TestClient(server.app)` runs the server's lifespan, and the
lifespan starts the preflight checks, which ran `claude --version` and
`claude auth status` for real: two processes for every client a test
opened, and some tests open five. It made the suite depend on the machine
running it, and those children, cancelled by the shutdown a moment later,
were where the "unclosed transport" warnings came from.

conftest's `_never_run_the_real_preflight` stands the checks down. This
holds it, with a `claude` of our own on PATH so that it holds on a machine
without one, where nothing would be spawned anyway.
"""

import importlib
import os
import shutil
import time
from pathlib import Path

from fastapi.testclient import TestClient


def test_starting_the_server_spawns_no_claude(monkeypatch, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / ("claude.cmd" if os.name == "nt" else "claude")
    fake.write_text("@echo off\n" if os.name == "nt" else "#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    assert Path(shutil.which("claude")).parent == bin_dir

    import process_tree
    spawned = []

    async def spawn(*args, **kwargs):
        spawned.append(args)
        raise OSError("recorded, never started")

    monkeypatch.setattr(process_tree, "spawn", spawn)

    import diagnostics_state
    monkeypatch.setattr(diagnostics_state, "checked_at", None)
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server
    importlib.reload(server)
    run_store.init_db()

    with TestClient(server.app):
        # The checks run in the background; wait until startup has recorded
        # them, so that anything they were going to spawn has been.
        deadline = time.monotonic() + 10
        while diagnostics_state.checked_at is None and time.monotonic() < deadline:
            time.sleep(0.01)
    assert diagnostics_state.checked_at is not None, "startup never recorded its checks"
    assert not spawned, f"starting the server under test spawned {spawned}"
