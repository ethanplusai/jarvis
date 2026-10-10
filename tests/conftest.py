import asyncio
import sys

import pytest
import pytest_asyncio


@pytest.fixture(autouse=True)
def _never_spawn_a_real_brain(monkeypatch):
    """server.lifespan builds the brain but must not start `claude` under test."""
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")


@pytest.fixture(autouse=True)
def _never_post_a_real_notification(monkeypatch, request):
    """No test may spam the developer's Notification Centre.

    Patched on the `notifier` module object itself rather than on `server`, so
    the `importlib.reload(server_module)` that several test fixtures do cannot
    hand the real implementation back. test_notifier.py is exempt: it tests
    notify() itself and mocks the subprocess boundary directly.
    """
    if request.module.__name__.endswith("test_notifier"):
        return
    import notifier

    async def _blocked(*args, **kwargs):
        raise AssertionError("a test tried to post a real macOS notification; "
                             "mock notifier.notify")

    monkeypatch.setattr(notifier, "notify", _blocked)


@pytest.fixture(autouse=True)
def _never_touch_the_real_projects_folder(monkeypatch, tmp_path):
    """No test may create a directory in the user's real ~/Projects.

    `create_project` writes into JARVIS_PROJECTS_DIR (default ~/Projects) and
    will create that root if it is missing, so the default is redirected into
    a tmp_path for every test — the same reasoning as JARVIS_DATA_DIR. A test
    that wants its own root still sets the variable itself; this only fills in
    a safe default.
    """
    monkeypatch.setenv("JARVIS_PROJECTS_DIR", str(tmp_path / "projects-root"))


@pytest.fixture(autouse=True)
def _never_write_to_the_live_dotenv(monkeypatch, tmp_path):
    """No test may write into the developer's live `.env`.

    Found the hard way: the settings endpoints write straight into the
    repository's own .env, so a test that posted a preference silently
    rewrote the developer's real configuration — and a test written to
    prove `.env` line injection injected the line for real. Same reasoning
    as JARVIS_DATA_DIR; a test that wants its own file still sets the
    variable itself.
    """
    monkeypatch.setenv("JARVIS_ENV_FILE", str(tmp_path / "dotenv" / ".env"))


@pytest.fixture(autouse=True)
def _never_write_to_the_live_data_dir(monkeypatch, tmp_path):
    """No test may write into the user's real `data/`.

    Most tests already set JARVIS_DATA_DIR (and still do — this only fills in
    a safe default), but a test that merely drives the brain writes there too
    now that a rate-limit event is persisted: without this, running the suite
    overwrote the live usage reading with a fixture's fake one.
    """
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data-dir"))


@pytest.fixture(autouse=True)
def _never_read_the_live_session_roster(monkeypatch, tmp_path):
    """No test may see the developer's own Claude Code sessions.

    The session watcher reads `~/.claude/sessions` and `~/.claude-orcha`,
    and a server started under TestClient starts the watcher. That passed on
    CI only because the runner has no live sessions; on a machine running
    Claude Code (as this suite usually is) the real ones appeared in
    "empty roster" assertions. The defaults are pointed at empty directories
    of the same names in tmp_path; a test that wants a roster builds one there
    or sets its own roots.
    """
    import session_watch
    home = tmp_path / "home"
    monkeypatch.setattr(session_watch, "DEFAULT_ROOTS",
                        (str(home / ".claude"), str(home / ".claude-orcha")))
    monkeypatch.delenv("JARVIS_CLAUDE_CONFIG_DIRS", raising=False)


@pytest_asyncio.fixture(autouse=True)
async def _no_run_left_mid_flight():
    """No test may end with a run's driver still starting its child.

    The CI hang, twice, on the macOS runner's Python 3.12 and never on 3.13:
    a test spawned a run, asserted on the row, and returned in the same
    millisecond — while `RunExecutor._drive` was still inside
    `asyncio.create_subprocess_exec`, before the child existed. The loop's
    teardown then cancelled that task mid-spawn, and 3.12's subprocess
    transport never completes a cancellation delivered there: the suite sat
    in `_cancel_all_tasks` until GitHub killed the job 25 minutes later.
    3.13 completes it, which is why no local run ever showed it.

    So every driver alive at the end of a test is waited for here, inside
    the test's own loop, before the runner closes it. A test's fake `claude`
    exits in milliseconds, so the wait is normally nothing; a driver that
    is queued or reading forever is cancelled only after it has had time to
    get past the spawn, which is the one place cancellation must not land.
    """
    yield
    me = asyncio.current_task()

    def _alive(qualname: str) -> list:
        return [t for t in asyncio.all_tasks()
                if t is not me and not t.done()
                and getattr(t.get_coro(), "__qualname__", "") == qualname]

    # First, the exact place: asyncio's own pipe-connection task, which
    # exists only between fork and "the child is up". Whoever spawned it
    # (a run driver, the brain, a fake `osascript`) is parked on it. Let it
    # finish — milliseconds — and yield once so the spawner moves on.
    connecting = _alive("BaseSubprocessTransport._connect_pipes")
    if connecting:
        await asyncio.wait(connecting, timeout=5)
        await asyncio.sleep(0)
    # Then a run driver still going: give it time to end on its own (a
    # test's fake claude exits at once) before it is cancelled somewhere
    # safe to cancel.
    drivers = _alive("RunExecutor._drive")
    if not drivers:
        return
    _done, pending = await asyncio.wait(drivers, timeout=10)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.wait(pending, timeout=5)


# Two things a test can ask of the filesystem that Windows will not do, and
# that are skips rather than failures there:
#   * a symlink, which needs Developer Mode or an elevated shell (1314); and
#   * a filename holding a newline, quote or other character NTFS forbids —
#     the "hostile filename" attacks, which cannot exist on that filesystem.
# Narrow on purpose: any other OSError still fails the test.
_NTFS_FORBIDDEN = set(chr(10) + chr(13) + '"<>|*?')


@pytest.hookimpl(wrapper=True)
def pytest_pyfunc_call(pyfuncitem):
    try:
        return (yield)
    except OSError as e:
        if sys.platform == "win32":
            if getattr(e, "winerror", None) == 1314:
                pytest.skip("creating a symlink needs Developer Mode on Windows")
            names = f"{e.filename or ''}{e.filename2 or ''}"
            if e.errno == 22 and _NTFS_FORBIDDEN & set(names):
                pytest.skip("filename characters NTFS cannot store")
        raise
