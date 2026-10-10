import asyncio
import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio


@pytest.fixture(autouse=True)
def _desktop_adapters_are_mocked(monkeypatch, request):
    """Legacy desktop fixtures model macOS; no test may touch the real desktop."""
    import actions
    import screen
    import notifier
    import dialog
    import windows_desktop
    if not request.module.__name__.endswith("test_windows_desktop"):
        for module in (actions, screen, notifier, dialog):
            monkeypatch.setattr(module, "_windows", False)
    async def blocked(*args, **kwargs):
        raise AssertionError("Mock the Windows desktop adapter before invoking it")
    if not request.module.__name__.endswith("test_windows_desktop"):
        for name in ("open_terminal", "open_browser", "open_editor", "capture_screen", "list_windows", "notify"):
            monkeypatch.setattr(windows_desktop, name, blocked)


@pytest.fixture(autouse=True)
def _never_spawn_a_real_brain(monkeypatch):
    """server.lifespan builds the brain but must not start `claude` under test."""
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    # Startup restricts the data directory's ACL with icacls; hundreds of
    # temp directories do not need that, and it is tested on its own.
    monkeypatch.setenv("JARVIS_HARDEN_PRIVATE_FILES", "0")


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
def _never_message_the_real_owner(monkeypatch, request):
    """No test may message the real owner, or read a real line, on either service.

    `envfile.load_once()` puts the repository's `.env` into `os.environ` at
    import, so a developer who has given JARVIS a number would otherwise
    have the suite text them every time an announcement path ran. The
    settings are removed, and the one function that talks to Kapso is
    blocked. tests/test_whatsapp.py is exempt from the block: it replaces
    the HTTP client with a MockTransport and sets the settings itself.
    """
    for key in ("KAPSO_API_KEY", "KAPSO_API_BASE_URL", "WHATSAPP_PHONE_NUMBER_ID",
                "WHATSAPP_OWNER_NUMBER", "WHATSAPP_OWNER_WA_ID", "WHATSAPP_TEMPLATE",
                "WHATSAPP_TEMPLATE_LANGUAGE", "WHATSAPP_INBOUND", "WHATSAPP_APPROVALS",
                "WHATSAPP_VOICE_NOTES", "TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID",
                "TELEGRAM_API_BASE_URL", "TELEGRAM_APPROVALS", "TELEGRAM_VOICE_NOTES"):
        monkeypatch.delenv(key, raising=False)
    module = request.module.__name__
    import telegram
    import whatsapp

    async def _blocked_whatsapp(*args, **kwargs):
        raise AssertionError("a test tried to call Kapso for real; mock whatsapp._request")

    async def _blocked_telegram(*args, **kwargs):
        raise AssertionError("a test tried to call Telegram for real; mock telegram._request")

    if not module.endswith("test_whatsapp"):
        monkeypatch.setattr(whatsapp, "_request", _blocked_whatsapp)
    if not module.endswith("test_telegram"):
        monkeypatch.setattr(telegram, "_request", _blocked_telegram)


@pytest.fixture(autouse=True)
def _never_run_the_real_preflight(monkeypatch, request):
    """Opening a TestClient must not run the developer's real `claude`.

    The server's lifespan starts the preflight checks, which spawn `claude
    --version` and `claude auth status` (and on macOS `osascript` and
    `security`), so every TestClient a test opened ran them for real. They
    are stood down here, and startup records that nothing was checked. The
    checks themselves are tested with fakes in test_preflight.py, which is
    exempt; their place in startup in test_preflight_runs_at_startup.py.
    Patched on the `preflight` module object, as `notifier.notify` is above,
    so no `importlib.reload(server_module)` hands the real one back. A test
    that wants particular results still patches `run_checks` itself.
    """
    if request.module.__name__.endswith("test_preflight"):
        return
    import preflight

    async def _nothing_checked(*args, **kwargs):
        return []

    monkeypatch.setattr(preflight, "run_checks", _nothing_checked)


@pytest.fixture(autouse=True)
def _never_run_the_real_codex(monkeypatch, request, tmp_path_factory):
    """No test may run the real Codex CLI, reach OpenAI, or spend the
    user's ChatGPT allowance.

    The fallback is off unless a test switches it on, what readiness last
    said is forgotten between tests, and the places Codex is looked for find
    nothing: a test that wants Codex hands `tests/fixtures/fake_codex.py` to
    `check_readiness` itself. test_chatgpt_fallback.py tests the search
    with directories of its own, so it keeps the real function.

    Nor is this machine's own machine-wide Codex configuration read: every
    readiness check looks for one, and no test's answer may depend on what
    C:\\ProgramData holds. The real search stays reachable, for its test.
    """
    for name in ("JARVIS_CHATGPT_FALLBACK", "JARVIS_CHATGPT_MODEL", "JARVIS_CODEX_PATH"):
        monkeypatch.delenv(name, raising=False)
    import chatgpt_fallback
    monkeypatch.setattr(chatgpt_fallback, "_readiness", None)
    monkeypatch.setattr(chatgpt_fallback, "_breach", None)
    monkeypatch.setattr(chatgpt_fallback, "_held", None)
    if not request.module.__name__.endswith("test_chatgpt_fallback"):
        monkeypatch.setattr(chatgpt_fallback, "codex_candidates", lambda: [])
    folder = tmp_path_factory.mktemp("programdata") / "OpenAI" / "Codex"
    monkeypatch.setattr(chatgpt_fallback, "real_system_config_folders",
                        chatgpt_fallback.system_config_folders, raising=False)
    monkeypatch.setattr(chatgpt_fallback, "system_config_folders", lambda: [folder])


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
    monkeypatch.setenv("JARVIS_PROJECT_ROOTS", str(tmp_path / "scan-root"))


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


@pytest.fixture(autouse=True)
def _symlinks_need_a_privilege_on_windows(monkeypatch):
    """A test that plants a symlink is skipped, not failed, where it cannot.

    Windows grants symlink creation only to administrators and to accounts
    with Developer Mode on; everyone else gets WinError 1314. The tests that
    plant one are testing what JARVIS does when it meets one, which is the
    same code on every platform, so a machine that cannot stage the setup
    skips with the reason rather than reporting a failure that is not one.
    """
    if sys.platform != "win32":
        return
    real_path = Path.symlink_to
    real_os = os.symlink

    def _skip_if_unprivileged(e):
        if getattr(e, "winerror", None) == 1314:
            pytest.skip("creating symlinks needs a privilege this account lacks")
        raise e

    def symlink_to(self, target, target_is_directory=False):
        try:
            return real_path(self, target, target_is_directory)
        except OSError as e:
            _skip_if_unprivileged(e)

    def symlink(src, dst, *args, **kwargs):
        try:
            return real_os(src, dst, *args, **kwargs)
        except OSError as e:
            _skip_if_unprivileged(e)

    monkeypatch.setattr(Path, "symlink_to", symlink_to)
    monkeypatch.setattr(os, "symlink", symlink)


@pytest.fixture(autouse=True)
def _never_read_the_real_session_roster(monkeypatch, tmp_path):
    """No test may read the developer's live Claude Code sessions.

    `session_watch.config_roots()` starts from `~/.claude` and
    `~/.claude-orcha`, and the server's lifespan starts a watcher over them,
    so a test that asked the projects view without stubbing the watcher was
    answered with whatever conversations the developer had open at the time.
    The defaults are pointed at empty directories with the same names; a
    test that wants its own roster still passes it explicitly, and
    JARVIS_CLAUDE_CONFIG_DIRS still adds to these as it always did.
    """
    import session_watch
    fake_home = tmp_path / "claude-config-home"
    monkeypatch.setattr(session_watch, "DEFAULT_ROOTS",
                        (str(fake_home / ".claude"), str(fake_home / ".claude-orcha")))


@pytest.fixture(autouse=True)
def _never_read_the_real_claude_settings(monkeypatch, tmp_path):
    """No test may read, or write, the developer's own `~/.claude/settings.json`.

    `preflight._settings_path()` falls back to `~/.claude`, so the steer
    replies depended on whose machine ran the suite: the developer's file
    says `"crossSessionInbound": "accept"` and the reply was "Passed to
    chitauri, sir."; a clean CI runner has no file, and the same reply grew
    the approve-it-first caveat. `enable_cross_session_inbound()` writes
    there, too. The default now points at an empty directory; a test that
    sets CLAUDE_CONFIG_DIR, or patches `_settings_path`, still gets its own.
    """
    import preflight
    fake_home = tmp_path / "claude-settings-home" / ".claude"

    def _settings_path():
        root = os.environ.get("CLAUDE_CONFIG_DIR")
        return (Path(root).expanduser() if root else fake_home) / "settings.json"

    monkeypatch.setattr(preflight, "_settings_path", _settings_path)
