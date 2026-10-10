"""The project scan must never run on the event loop.

`scan_projects` was `async def` with a wholly synchronous body — an iterdir()
over ~/Desktop and a read_text() on every repo's .git/HEAD. Nothing awaited,
so it ran to completion on the event loop thread and `/api/specs` and
`/api/projects` froze the entire server, voice WebSocket included, for as long
as the walk took. On a cloud-synced Desktop that was over ten minutes.
"""

import asyncio
import sys
import time
from pathlib import Path

import os
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import server


@pytest.fixture(autouse=True)
def _clear_scan_cache():
    server._scan_cache.update(at=0.0, value=[])
    yield
    server._scan_cache.update(at=0.0, value=[])


def _make_repo(root: Path, name: str, branch: str = "main") -> None:
    git = root / name / ".git"
    git.mkdir(parents=True)
    (git / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")


def test_a_slow_scan_does_not_freeze_the_event_loop(monkeypatch, tmp_path):
    """The regression itself: the loop must keep running during a scan."""
    _make_repo(tmp_path, "alpha")
    monkeypatch.setenv("JARVIS_PROJECT_ROOTS", str(tmp_path))

    real = server._scan_projects_blocking

    def slow(deadline):
        time.sleep(0.5)          # stands in for a very slow filesystem
        return real(deadline)

    monkeypatch.setattr(server, "_scan_projects_blocking", slow)

    async def go():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        projects = await server.scan_projects()
        beat.cancel()
        return projects, ticks

    projects, ticks = asyncio.run(go())
    assert [p["name"] for p in projects] == ["alpha"]
    # If the scan ran on the loop, the heartbeat could not have ticked at all.
    assert ticks > 5, f"event loop was blocked during the scan (ticks={ticks})"


def test_the_scan_stops_itself_at_the_budget(monkeypatch, tmp_path):
    """A thread from asyncio.to_thread cannot be cancelled, so the walk has to
    honour the deadline itself or it runs on regardless."""
    for i in range(50):
        _make_repo(tmp_path, f"repo{i:03d}")
    monkeypatch.setenv("JARVIS_PROJECT_ROOTS", str(tmp_path))

    real_is_dir = Path.is_dir

    def slow_is_dir(self):
        time.sleep(0.01)
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", slow_is_dir)
    monkeypatch.setattr(server, "SCAN_BUDGET_SECONDS", 0.1)

    started = time.monotonic()
    projects = asyncio.run(server.scan_projects())
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"budget not honoured, took {elapsed:.1f}s"
    assert len(projects) < 50, "should have stopped early, not walked everything"


def test_a_partial_scan_is_not_cached_as_the_whole_picture(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_PROJECT_ROOTS", str(tmp_path))
    monkeypatch.setattr(
        server, "_scan_projects_blocking",
        lambda deadline: ([{"name": "partial", "path": "p", "branch": "main"}], False))

    asyncio.run(server.scan_projects())
    assert server._scan_cache["value"] == [], "a partial answer must not be cached"


def test_a_complete_scan_is_served_from_cache(monkeypatch, tmp_path):
    _make_repo(tmp_path, "alpha")
    monkeypatch.setenv("JARVIS_PROJECT_ROOTS", str(tmp_path))

    calls = 0
    real = server._scan_projects_blocking

    def counting(deadline):
        nonlocal calls
        calls += 1
        return real(deadline)

    monkeypatch.setattr(server, "_scan_projects_blocking", counting)

    asyncio.run(server.scan_projects())
    asyncio.run(server.scan_projects())
    assert calls == 1, "the second call should have come from the cache"


def test_roots_are_overridable(monkeypatch, tmp_path):
    """A user whose Desktop is slow or cloud-backed needs an escape hatch."""
    monkeypatch.setenv("JARVIS_PROJECT_ROOTS",
                       os.pathsep.join([str(tmp_path), str(tmp_path / 'nope')]))
    assert server._scan_roots() == [tmp_path, tmp_path / "nope", server.project_maker.projects_root()]

    monkeypatch.delenv("JARVIS_PROJECT_ROOTS", raising=False)
    assert server.DESKTOP_PATH in server._scan_roots()
