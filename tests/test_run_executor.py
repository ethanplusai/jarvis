import asyncio
import importlib
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

import procs

sys.path.insert(0, str(Path(__file__).parent.parent))

FIXTURE = Path(__file__).parent / "fixtures" / "stream_success.jsonl"


def _fake_claude(tmp_path: Path, fixture: Path, exit_code: int = 0,
                 sleep_sec: float = 0.0) -> str:
    """A stand-in `claude` binary that replays a fixture then exits."""
    script = tmp_path / "fake_claude.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        f"time.sleep({sleep_sec})\n"
        f"sys.stdout.write(open({str(fixture)!r}).read())\n"
        "sys.stdout.flush()\n"
        f"sys.exit({exit_code})\n"
    , encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return f"{sys.executable} {script}"


def _fake_claude_lines(tmp_path: Path, lines: list[str], exit_code: int = 0) -> str:
    """A stand-in `claude` binary that writes exactly these lines, verbatim,
    then exits. Used to drive oversized-line handling through the real
    asyncio subprocess reading path (no mocks)."""
    payload = tmp_path / f"fake_claude_payload_{len(lines)}_{id(lines)}.txt"
    payload.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script = tmp_path / f"fake_claude_lines_{len(lines)}_{id(lines)}.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stdout.write(open({str(payload)!r}).read())\n"
        "sys.stdout.flush()\n"
        f"sys.exit({exit_code})\n"
    , encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return f"{sys.executable} {script}"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    run_store.init_db()
    import run_executor
    importlib.reload(run_executor)
    return run_store, run_executor, tmp_path


@pytest.mark.asyncio
async def test_spawn_returns_run_id_and_persists(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_fake_claude(tmp, FIXTURE))
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    assert store.get_run(run_id) is not None


@pytest.mark.asyncio
async def test_successful_run_reaches_succeeded(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_fake_claude(tmp, FIXTURE))
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    run = await ex.wait_for(run_id)
    assert run["status"] == store.RunStatus.SUCCEEDED
    assert run["exit_code"] == 0
    assert run["ended_at"] is not None


@pytest.mark.asyncio
async def test_cost_and_tokens_captured(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_fake_claude(tmp, FIXTURE))
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    run = await ex.wait_for(run_id)
    assert run["cost_usd"] > 0
    assert run["input_tokens"] == 2
    assert run["output_tokens"] == 4
    assert run["cache_read_tokens"] == 10143
    assert run["num_turns"] == 1
    assert run["result_text"] == "OK"


@pytest.mark.asyncio
async def test_events_persisted_in_order(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_fake_claude(tmp, FIXTURE))
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    await ex.wait_for(run_id)
    events = store.get_events(run_id, limit=100)
    assert len(events) == 14
    assert [e["seq"] for e in events] == list(range(1, 15))
    assert events[-1]["kind"] == "result"


@pytest.mark.asyncio
async def test_nonzero_exit_marks_failed(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_fake_claude(tmp, FIXTURE, exit_code=3))
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    run = await ex.wait_for(run_id)
    assert run["status"] == store.RunStatus.FAILED
    assert run["exit_code"] == 3


@pytest.mark.asyncio
async def test_subscribers_receive_lifecycle_messages(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_fake_claude(tmp, FIXTURE))
    seen = []
    ex.subscribe(lambda msg: seen.append(msg["type"]))
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    await ex.wait_for(run_id)
    assert "run_started" in seen
    assert "run_event" in seen
    assert "run_finished" in seen


@pytest.mark.asyncio
async def test_subscriber_exception_does_not_break_run(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_fake_claude(tmp, FIXTURE))

    def boom(msg):
        raise RuntimeError("subscriber exploded")

    ex.subscribe(boom)
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    run = await ex.wait_for(run_id)
    assert run["status"] == store.RunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_run_updated_published_for_model_and_cost(env):
    """Cost, tokens and model must reach the dashboard while the run is still
    running. `run_updated` is handled in dashboard/live.ts, but for a while
    nothing published it, so the UI showed "—" and "$0.0000" until
    run_finished. It fires only on events that actually change the row:
    system/init (model), each assistant turn that reports token usage, and
    result (cost/usage). The fixture has one of each."""
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_fake_claude(tmp, FIXTURE))
    seen = []
    ex.subscribe(lambda msg: seen.append(msg))
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    await ex.wait_for(run_id)

    updates = [m for m in seen if m["type"] == "run_updated"]
    events = [m for m in seen if m["type"] == "run_event"]
    assert len(updates) == 3, (
        "expected one update for system/init, one for the assistant turn's "
        "usage, and one for result"
    )
    assert len(events) == 14 and len(updates) < len(events), (
        "publishing per streamed event would be one message per line — three "
        "quarters of them hook plumbing"
    )
    assert updates[0]["run"]["model"]
    assert updates[1]["run"]["output_tokens"] == 1     # climbing, mid-run
    assert updates[2]["run"]["cost_usd"] > 0
    assert updates[2]["run"]["output_tokens"] == 4     # the result's own total


@pytest.mark.asyncio
async def test_run_updated_carries_the_full_row_not_a_delta(env):
    """The dashboard replaces its row wholesale from `msg.run`, exactly as it
    does for run_started/run_finished."""
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_fake_claude(tmp, FIXTURE))
    seen = []
    ex.subscribe(lambda msg: seen.append(msg))
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    await ex.wait_for(run_id)

    started = [m for m in seen if m["type"] == "run_started"][0]
    updates = [m for m in seen if m["type"] == "run_updated"]
    assert updates
    for update in updates:
        assert update["run"]["id"] == run_id
        assert set(update["run"]) == set(started["run"])


@pytest.mark.asyncio
async def test_run_updated_precedes_run_finished(env):
    """Store-then-publish: every update describes a row already written, and
    all of them land before the terminal message."""
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_fake_claude(tmp, FIXTURE))
    seen = []
    ex.subscribe(lambda msg: seen.append(msg["type"]))
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    await ex.wait_for(run_id)

    assert seen.index("run_finished") == len(seen) - 1
    assert seen.index("run_started") < seen.index("run_updated")


@pytest.mark.asyncio
async def test_model_recorded_from_init_event(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_fake_claude(tmp, FIXTURE))
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    run = await ex.wait_for(run_id)
    assert run["model"]


@pytest.mark.asyncio
async def test_missing_binary_marks_failed(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path="/nonexistent/claude-binary")
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    run = await ex.wait_for(run_id)
    assert run["status"] == store.RunStatus.FAILED
    assert run["error"]


# --------------------------------------------------------------------------
# Regression tests for the async-lifecycle defects (task 5).
# Each of these fails against the original implementation.
# --------------------------------------------------------------------------


def _script(tmp_path: Path, name: str, body: str) -> str:
    """Write a stand-in `claude` binary with an arbitrary body."""
    script = tmp_path / name
    script.write_text("#!/usr/bin/env python3\nimport sys, time\n" + body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    # Windows venv python.exe is a launcher that itself holds the child's
    # pipes open. These fixtures need the actual interpreter to model EOF.
    return f"{getattr(sys, '_base_executable', sys.executable)} {script}"


def _slow_claude(tmp_path: Path, name: str = "slow_claude.py",
                 sleep_sec: float = 30.0) -> str:
    """Emits one event, then blocks — so the run is reliably still running."""
    return _script(tmp_path, name,
                   "sys.stdout.write(open(%r).readline())\n"
                   "sys.stdout.flush()\n"
                   "time.sleep(%r)\n" % (str(FIXTURE), sleep_sec))


async def _await_status(store, run_id: str, status: str,
                        timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = store.get_run(run_id)
        if run and run["status"] == status:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"run never reached {status!r}; last={store.get_run(run_id)}")


# -- CRITICAL 1: cancel() must record CANCELLED, not failed/-15 -------------

@pytest.mark.asyncio
async def test_cancel_running_run_records_cancelled(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_slow_claude(tmp), grace_sec=5)
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    await _await_status(store, run_id, store.RunStatus.RUNNING)

    assert await ex.cancel(run_id) is True

    run = await ex.wait_for(run_id, timeout=15)
    assert run["status"] == store.RunStatus.CANCELLED, run
    assert run["ended_at"] is not None


@pytest.mark.asyncio
async def test_cancel_is_false_for_unknown_and_terminal_runs(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_fake_claude(tmp, FIXTURE))
    assert await ex.cancel("no-such-run") is False
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    await ex.wait_for(run_id)
    assert await ex.cancel(run_id) is False


# -- CRITICAL 2: a burst must not deadlock on the concurrency gate ----------

@pytest.mark.asyncio
async def test_burst_over_max_concurrent_all_complete(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_fake_claude(tmp, FIXTURE,
                                                         sleep_sec=0.05),
                         max_concurrent=3)
    live, peak = 0, 0

    def watch(msg):
        nonlocal live, peak
        if msg["type"] == "run_started":
            live += 1
            peak = max(peak, live)
        elif msg["type"] == "run_finished":
            live -= 1

    ex.subscribe(watch)
    ids = [await ex.spawn("do a thing", "proj", str(tmp), "api")
           for _ in range(5)]
    runs = await asyncio.gather(*(ex.wait_for(i, timeout=20) for i in ids))

    assert [r["status"] for r in runs] == [store.RunStatus.SUCCEEDED] * 5
    assert peak <= 3, f"concurrency limit exceeded: {peak}"


# -- CRITICAL 3: an exception mid-stream must still reach a terminal state --

class _ExplodingStore:
    """Delegates to the real store, but append_events always raises."""

    def __init__(self, store):
        self._store = store

    def __getattr__(self, name):
        return getattr(self._store, name)

    def append_events(self, run_id, rows):
        raise RuntimeError("database is locked")


@pytest.mark.asyncio
async def test_store_failure_still_reaches_terminal_state(env):
    store, mod, tmp = env
    # Streams events (so the store write is attempted), then blocks — a child
    # that is not killed on the failure path stays alive.
    claude = _script(
        tmp, "streaming_then_blocking_claude.py",
        f"sys.stdout.write(open({str(FIXTURE)!r}).read())\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n",
    )
    ex = mod.RunExecutor(_ExplodingStore(store), claude_path=claude,
                         grace_sec=5)
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    await _await_status(store, run_id, store.RunStatus.RUNNING)
    pid = store.get_run(run_id)["pid"]

    run = await ex.wait_for(run_id, timeout=15)
    assert run["status"] in store.RunStatus.TERMINAL, run
    assert run["status"] == store.RunStatus.FAILED
    assert run["ended_at"] is not None
    assert "database is locked" in run["error"]

    # ...and no child left behind.
    assert ex._procs == {}
    assert not _alive(pid)


# -- IMPORTANT 4: stderr must be drained concurrently ----------------------

@pytest.mark.asyncio
async def test_large_stderr_does_not_deadlock(env):
    store, mod, tmp = env
    claude = _script(
        tmp, "chatty_claude.py",
        "sys.stderr.write('E' * 500_000)\n"
        "sys.stderr.write('TAIL-MARKER')\n"
        "sys.stderr.flush()\n"
        f"sys.stdout.write(open({str(FIXTURE)!r}).read())\n"
        "sys.stdout.flush()\n"
        "sys.exit(3)\n",
    )
    ex = mod.RunExecutor(store, claude_path=claude)
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")

    run = await ex.wait_for(run_id, timeout=20)
    assert run["status"] == store.RunStatus.FAILED
    assert run["exit_code"] == 3
    # Only the tail is kept, and it is capped.
    assert run["error"].endswith("TAIL-MARKER")
    assert len(run["error"]) <= 2000


# -- IMPORTANT 5: a queued run must be cancellable ------------------------

@pytest.mark.asyncio
async def test_cancel_queued_run_returns_true_and_never_spawns(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_slow_claude(tmp, sleep_sec=5),
                         max_concurrent=1, grace_sec=5)
    first = await ex.spawn("first", "proj", str(tmp), "api")
    await _await_status(store, first, store.RunStatus.RUNNING)

    queued = await ex.spawn("second", "proj", str(tmp), "api")
    await asyncio.sleep(0.1)
    assert store.get_run(queued)["status"] == store.RunStatus.QUEUED

    assert await ex.cancel(queued) is True
    assert store.get_run(queued)["status"] == store.RunStatus.CANCELLED

    # It must never have started a process, even after the slot frees up.
    assert await ex.cancel(first) is True
    await asyncio.sleep(0.3)
    run = store.get_run(queued)
    assert run["status"] == store.RunStatus.CANCELLED
    assert run["pid"] is None
    assert run["started_at"] is None
    assert store.get_events(queued, limit=10) == []


@pytest.mark.asyncio
async def test_cancelling_a_queued_run_is_not_logged_as_an_error(env, caplog):
    """Cancelling a queued run cancels its driver task on purpose. That lands
    in the BaseException handler as CancelledError, where it used to be logged
    via log.exception("run ... driver failed") at ERROR — a stack trace for a
    normal user action."""
    import logging
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_slow_claude(tmp, sleep_sec=5),
                         max_concurrent=1, grace_sec=5)
    first = await ex.spawn("first", "proj", str(tmp), "api")
    await _await_status(store, first, store.RunStatus.RUNNING)
    queued = await ex.spawn("second", "proj", str(tmp), "api")
    await asyncio.sleep(0.1)

    with caplog.at_level(logging.INFO, logger="jarvis.run_executor"):
        assert await ex.cancel(queued) is True
        await ex.cancel(first)
        await asyncio.sleep(0.3)

    errors = [r for r in caplog.records
              if r.levelno >= logging.ERROR and queued in r.getMessage()]
    assert errors == [], f"queued cancel logged at ERROR: {errors}"
    assert any(r.levelno == logging.INFO and "cancelled while queued"
               in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# Task 6 additions.
#
# Several tests from the task-6 brief duplicate assertions already made by
# the task-5 regression tests above (added in 551c90a) and are intentionally
# NOT repeated here:
#
#   - test_cancel_marks_cancelled            -> test_cancel_running_run_records_cancelled
#   - test_cancel_unknown_run_returns_false  -> test_cancel_is_false_for_unknown_and_terminal_runs
#   - test_zero_timeout_means_no_timeout     -> test_successful_run_reaches_succeeded
#         (spawn()'s timeout_sec defaults to 0, and that test already spawns
#         without passing timeout_sec and reaches SUCCEEDED)
#   - test_terminal_status_is_not_overwritten -> test_cancel_running_run_records_cancelled
#         and test_store_failure_still_reaches_terminal_state
#         (both already require the race between cancel()'s direct _finish()
#         call and _drive()'s own terminal write to resolve to a single,
#         non-overwritten status; _finish()'s "already terminal -> no-op"
#         guard is exercised by both)
#   - test_concurrency_limit_holds_runs_queued -> test_burst_over_max_concurrent_all_complete
#         (peak concurrent <= max_concurrent) combined with
#         test_cancel_queued_run_returns_true_and_never_spawns (a run beyond
#         the limit is held in QUEUED)
#
# The brief's note about `_await_slot` and an "activE_count() - 1" off-by-one
# is obsolete: that polling gate was replaced by `asyncio.Semaphore` in
# 551c90a, so there is nothing left to test there.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_marks_timed_out(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_slow_claude(tmp), grace_sec=1.0)
    run_id = await ex.spawn("hang", "proj", str(tmp), "api", timeout_sec=1)
    run = await ex.wait_for(run_id, timeout=20)
    assert run["status"] == store.RunStatus.TIMED_OUT
    assert "timeout" in run["error"]


@pytest.mark.asyncio
async def test_timeout_keeps_the_collected_stderr(env, tmp_path):
    """The timeout path collected stderr and threw it away. A killed run is
    the one that most needs the context."""
    store, mod, tmp = env
    script = tmp_path / "noisy_hang.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "sys.stderr.write('ENOSPC: no space left on device\\n')\n"
        "sys.stderr.flush()\n"
        "time.sleep(30)\n"
    , encoding="utf-8")
    ex = mod.RunExecutor(store, claude_path=f"{sys.executable} {script}",
                         grace_sec=1.0)
    run_id = await ex.spawn("hang", "proj", str(tmp), "api", timeout_sec=1)
    run = await ex.wait_for(run_id, timeout=20)

    assert run["status"] == store.RunStatus.TIMED_OUT
    assert "exceeded timeout" in run["error"]
    assert "ENOSPC: no space left on device" in run["error"]


# -- the four `_command` construction tests: the highest-value tests here --
# Nothing else in the suite exercises how the command line is built, and two
# of its flags are load-bearing: `--dangerously-skip-permissions` (without it
# a spawned run has no TTY to answer a permission prompt and hangs forever)
# and `--fork-session` alongside `--resume` (CLI 2.1.251 rejects
# `--session-id` with `--resume` otherwise, so every retry would fail).

def test_command_includes_skip_permissions_by_default(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path="claude")
    cmd = ex._command("run-1", None)
    assert "--dangerously-skip-permissions" in cmd, (
        "without this flag a run blocks on a permission prompt it has no TTY "
        "to answer, and hangs forever")


def test_command_sets_session_id(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path="claude")
    cmd = ex._command("run-1", None)
    assert cmd[cmd.index("--session-id") + 1] == "run-1"
    assert "--resume" not in cmd
    assert "--fork-session" not in cmd


def test_command_forks_when_resuming(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path="claude")
    cmd = ex._command("run-2", "run-1")
    assert cmd[cmd.index("--resume") + 1] == "run-1"
    assert "--fork-session" in cmd, (
        "CLI 2.1.251 rejects --session-id with --resume unless --fork-session "
        "is also passed")


def test_command_respects_skip_permissions_opt_out(env, monkeypatch):
    store, mod, tmp = env
    monkeypatch.setenv("JARVIS_SKIP_PERMISSIONS", "false")
    importlib.reload(mod)
    ex = mod.RunExecutor(store, claude_path="claude")
    assert "--dangerously-skip-permissions" not in ex._command("run-1", None)


# -- explicit model selection ------------------------------------------------
# --model is now always explicit: explicit argument, else JARVIS_RUN_MODEL,
# else "sonnet" — and the flag itself must never be omitted, since relying on
# the CLI's own default silently changed behaviour once before (brain.py's
# BrainConfig sets the same precedent for the voice path).

def test_command_always_includes_an_explicit_model_flag(env, monkeypatch):
    store, mod, tmp = env
    monkeypatch.delenv("JARVIS_RUN_MODEL", raising=False)
    ex = mod.RunExecutor(store, claude_path="claude")
    cmd = ex._command("run-1", None)
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "sonnet"


def test_command_uses_explicit_model_argument(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path="claude")
    cmd = ex._command("run-1", None, model="haiku")
    assert cmd[cmd.index("--model") + 1] == "haiku"


def test_command_falls_back_to_env_model(env, monkeypatch):
    store, mod, tmp = env
    monkeypatch.setenv("JARVIS_RUN_MODEL", "opus")
    ex = mod.RunExecutor(store, claude_path="claude")
    cmd = ex._command("run-1", None)
    assert cmd[cmd.index("--model") + 1] == "opus"


def test_command_explicit_argument_overrides_env(env, monkeypatch):
    store, mod, tmp = env
    monkeypatch.setenv("JARVIS_RUN_MODEL", "opus")
    ex = mod.RunExecutor(store, claude_path="claude")
    cmd = ex._command("run-1", None, model="haiku")
    assert cmd[cmd.index("--model") + 1] == "haiku"


def _argv_capturing_claude(tmp_path: Path, marker: Path,
                           fixture: Path = FIXTURE) -> str:
    """A stand-in `claude` that records its own argv, then replays a
    fixture — proves the --model flag actually reaches the child process,
    the way test_prompt_is_delivered_via_stdin proves the prompt does."""
    return _script(
        tmp_path, "argv_capturing_claude.py",
        "import json\n"
        f"json.dump(sys.argv[1:], open({str(marker)!r}, 'w'))\n"
        f"sys.stdout.write(open({str(fixture)!r}).read())\n"
        "sys.stdout.flush()\n"
    )


@pytest.mark.asyncio
async def test_explicit_model_reaches_the_child_argv(env):
    store, mod, tmp = env
    marker = tmp / "argv.json"
    ex = mod.RunExecutor(store, claude_path=_argv_capturing_claude(tmp, marker))
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api", model="haiku")
    run = await ex.wait_for(run_id)

    assert run["status"] == store.RunStatus.SUCCEEDED
    argv = json.loads(marker.read_text(encoding="utf-8"))
    assert argv[argv.index("--model") + 1] == "haiku"


@pytest.mark.asyncio
async def test_spawn_without_model_falls_back_to_env(env, monkeypatch):
    store, mod, tmp = env
    monkeypatch.setenv("JARVIS_RUN_MODEL", "opus")
    marker = tmp / "argv.json"
    ex = mod.RunExecutor(store, claude_path=_argv_capturing_claude(tmp, marker))
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    await ex.wait_for(run_id)

    argv = json.loads(marker.read_text(encoding="utf-8"))
    assert argv[argv.index("--model") + 1] == "opus"


@pytest.mark.asyncio
async def test_spawn_with_neither_falls_back_to_sonnet_and_flag_always_present(
        env, monkeypatch):
    store, mod, tmp = env
    monkeypatch.delenv("JARVIS_RUN_MODEL", raising=False)
    marker = tmp / "argv.json"
    ex = mod.RunExecutor(store, claude_path=_argv_capturing_claude(tmp, marker))
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    await ex.wait_for(run_id)

    argv = json.loads(marker.read_text(encoding="utf-8"))
    assert "--model" in argv, "--model must never be omitted"
    assert argv[argv.index("--model") + 1] == "sonnet"


@pytest.mark.asyncio
async def test_requested_model_persisted_before_the_process_even_spawns(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_slow_claude(tmp),
                         max_concurrent=1, grace_sec=1.0)
    first = await ex.spawn("hang 1", "proj", str(tmp), "api", model="opus")
    # Second run is still queued — never spawned a process — yet its
    # requested model must already be on the row.
    second = await ex.spawn("hang 2", "proj", str(tmp), "api", model="haiku")
    assert store.get_run(second)["status"] == store.RunStatus.QUEUED
    assert store.get_run(second)["requested_model"] == "haiku"
    assert store.get_run(first)["requested_model"] == "opus"

    await ex.cancel(first)
    await ex.cancel(second)


@pytest.mark.asyncio
async def test_requested_model_comes_back_on_the_finished_run(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_fake_claude(tmp, FIXTURE))
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api", model="haiku")
    run = await ex.wait_for(run_id)
    assert run["status"] == store.RunStatus.SUCCEEDED
    assert run["requested_model"] == "haiku"


@pytest.mark.asyncio
async def test_terminal_state_invariant_holds_with_explicit_model(env):
    """Both invariants apply regardless of which model was requested: a run
    still always reaches a terminal state, success or failure."""
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_fake_claude(tmp, FIXTURE, exit_code=1))
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api", model="opus")
    run = await ex.wait_for(run_id)
    assert run["status"] in store.RunStatus.TERMINAL
    assert run["status"] == store.RunStatus.FAILED
    assert run["requested_model"] == "opus"


@pytest.mark.asyncio
async def test_events_are_batched_not_written_per_event(env, monkeypatch):
    store, mod, tmp = env
    calls = []
    original = store.append_events

    def counting(run_id, rows):
        calls.append(len(rows))
        return original(run_id, rows)

    monkeypatch.setattr(store, "append_events", counting)
    ex = mod.RunExecutor(store, claude_path=_fake_claude(tmp, FIXTURE))
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    await ex.wait_for(run_id)

    assert len(store.get_events(run_id, limit=100)) == 14
    assert len(calls) <= 3, f"expected batching, got {len(calls)} writes"


@pytest.mark.asyncio
async def test_finished_runs_leave_the_task_table(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_fake_claude(tmp, FIXTURE))
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    await ex.wait_for(run_id)
    await asyncio.sleep(0.05)
    assert run_id not in ex._tasks
    assert ex.active_count() == 0


@pytest.mark.asyncio
async def test_queued_run_starts_when_slot_frees(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_slow_claude(tmp),
                         max_concurrent=1, grace_sec=1.0)
    first = await ex.spawn("hang 1", "proj", str(tmp), "api")
    second = await ex.spawn("hang 2", "proj", str(tmp), "api")

    await _await_status(store, first, store.RunStatus.RUNNING)
    assert store.get_run(second)["status"] == store.RunStatus.QUEUED

    await ex.cancel(first)

    await _await_status(store, second, store.RunStatus.RUNNING)
    assert store.get_run(second)["status"] == store.RunStatus.RUNNING
    await ex.cancel(second)


# -- not in the brief: prove the prompt is actually delivered --------------
# The fake `claude` binaries used above ignore both argv and stdin, so
# nothing above proves the prompt text ever reaches the child process. A
# silently undelivered prompt would mean Claude Code runs with no
# instructions at all, and the failure mode would be baffling: the run
# would still stream valid-looking stream-json and reach SUCCEEDED.

@pytest.mark.asyncio
async def test_prompt_is_delivered_via_stdin(env):
    store, mod, tmp = env
    marker = tmp / "received_prompt.txt"
    claude = _script(
        tmp, "stdin_capturing_claude.py",
        f"open({str(marker)!r}, 'w').write(sys.stdin.read())\n",
    )
    ex = mod.RunExecutor(store, claude_path=claude)
    run_id = await ex.spawn("the secret prompt text", "proj", str(tmp), "api")
    run = await ex.wait_for(run_id)

    assert run["status"] == store.RunStatus.SUCCEEDED
    assert marker.read_text(encoding="utf-8") == "the secret prompt text"


# ---------------------------------------------------------------------------
# The environment a run is given.
#
# The brain has run on a scrubbed environment since milestone 1; the run
# pipeline did not, and inherited the server's. server.py loads .env into
# os.environ at import, and a developer's .env legitimately holds
# ANTHROPIC_API_KEY for the older lookup paths. The CLI silently PREFERS an
# inherited key over the subscription login — `claude auth status` still says
# loggedIn: true while apiKeySource flips to the env key and the account's
# email and organisation go blank — so every run JARVIS spawned was billed to
# the paid API instead of the user's subscription, with nothing to show for it
# in the output. Nothing here should ever be able to regress to inheriting.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_run_never_inherits_api_credentials(env, monkeypatch):
    store, mod, tmp = env
    marker = tmp / "child_env.json"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-never-reach-a-run")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://elsewhere.example")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "ws-123")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "the-server's-own")
    monkeypatch.setenv("CLAUDECODE", "1")

    claude = _script(
        tmp, "env_dumping_claude.py",
        "import json, os\n"
        f"open({str(marker)!r}, 'w').write(json.dumps(dict(os.environ)))\n"
        f"sys.stdout.write(open({str(FIXTURE)!r}).read())\n"
        "sys.stdout.flush()\n",
    )
    ex = mod.RunExecutor(store, claude_path=claude)
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "voice")
    run = await ex.wait_for(run_id)

    assert run["status"] == store.RunStatus.SUCCEEDED
    child = json.loads(marker.read_text(encoding="utf-8"))
    leaked = [k for k in child
              if k.startswith(("ANTHROPIC_", "CLAUDE_CODE_"))
              or k == "CLAUDECODE"]
    assert leaked == [], (
        f"a spawned run inherited {leaked} — it will be billed to the API key "
        f"instead of the user's subscription")


@pytest.mark.asyncio
async def test_a_run_never_inherits_the_session_that_started_jarvis(env, monkeypatch):
    """Measured 2026-09-26: JARVIS started by an agent inside Claude Code
    carried that session's own variables, and the scrub passed on all but
    CLAUDE_CODE_* and ANTHROPIC_*. A run is an unattended agent with
    permissions skipped; it gets JARVIS's choices, not the launcher's."""
    store, mod, tmp = env
    marker = tmp / "child_env_session.json"
    leaked = {
        "CLAUDE_EFFORT": "xhigh", "CLAUDE_PID": "4242",
        "AI_AGENT": "claude-code_2-1-280_agent",
        "CLAUDE_AGENT_SDK_VERSION": "0.3.280",
        "MCP_CONNECTION_NONBLOCKING": "true", "MCP_SERVER_CONNECTION_BATCH_SIZE": "8",
        "API_TIMEOUT_MS": "900000", "USE_STAGING_OAUTH": "1",
        "TRACEPARENT": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://collector.example",
        "OTEL_EXPORTER_OTLP_HEADERS": "authorization=Bearer collector-secret",
    }
    for name, value in leaked.items():
        monkeypatch.setenv(name, value)

    claude = _script(
        tmp, "env_dumping_claude_3.py",
        "import json, os\n"
        f"open({str(marker)!r}, 'w').write(json.dumps(dict(os.environ)))\n"
        f"sys.stdout.write(open({str(FIXTURE)!r}).read())\n"
        "sys.stdout.flush()\n",
    )
    ex = mod.RunExecutor(store, claude_path=claude)
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "voice")
    run = await ex.wait_for(run_id)

    assert run["status"] == store.RunStatus.SUCCEEDED
    child = json.loads(marker.read_text(encoding="utf-8"))
    assert sorted(set(child) & set(leaked)) == []


@pytest.mark.asyncio
async def test_a_run_still_gets_the_ordinary_environment(env, monkeypatch):
    """The scrub is a scalpel, not a bucket: a child with no PATH or HOME
    cannot find git, node, or the user's own Claude configuration."""
    store, mod, tmp = env
    marker = tmp / "child_env_ordinary.json"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/keep/me")
    # Without it a Windows run whose git is not where the CLI looks has no
    # Bash tool at all; the old CLAUDE_CODE_* scrub removed it.
    monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", "/keep/bash.exe")
    monkeypatch.setenv("JARVIS_MARKER", "kept")
    monkeypatch.setenv("HOME", str(tmp))

    claude = _script(
        tmp, "env_dumping_claude_2.py",
        "import json, os\n"
        f"open({str(marker)!r}, 'w').write(json.dumps(dict(os.environ)))\n"
        f"sys.stdout.write(open({str(FIXTURE)!r}).read())\n"
        "sys.stdout.flush()\n",
    )
    ex = mod.RunExecutor(store, claude_path=claude)
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "voice")
    await ex.wait_for(run_id)

    child = json.loads(marker.read_text(encoding="utf-8"))
    assert child.get("PATH") == os.environ["PATH"]
    assert child.get("HOME") == os.environ["HOME"]
    assert child.get("CLAUDE_CONFIG_DIR") == "/keep/me"
    assert child.get("CLAUDE_CODE_GIT_BASH_PATH") == "/keep/bash.exe"
    assert child.get("JARVIS_MARKER") == "kept"


def test_the_scrub_is_shared_with_the_brain_not_copied():
    """One copy of the rule. The first copy of it was fixed for the brain and
    left broken here for a whole milestone; a second copy invites the same
    thing again."""
    import brain
    import claude_env
    import run_executor as mod

    assert brain.SCRUBBED_ENV_PREFIXES is claude_env.SCRUBBED_ENV_PREFIXES
    assert brain.Brain.child_env() == claude_env.child_env()
    assert mod.claude_env is claude_env


# ── oversized stream-json lines (asyncio's 64 KiB StreamReader default) ────
#
# `asyncio.create_subprocess_exec` defaults its stdout/stderr StreamReader to
# a 64 KiB line limit. A single stream-json line carrying a large tool result
# routinely exceeds that; readline() then raises ValueError, which — before
# this fix — propagated out of `_consume` and killed an otherwise-healthy
# run. A real run once died 27.9 minutes in with exactly this: 'failed',
# ValueError('Separator is not found, and chunk exceed the limit').

@pytest.mark.asyncio
async def test_line_well_over_64kib_is_parsed_correctly(env):
    """Proves the raised limit: a real subprocess emits a single JSON line
    far bigger than asyncio's old 64 KiB default (but comfortably under the
    raised limit), and the run must still succeed with that line's content
    intact in the persisted events."""
    store, mod, tmp = env
    marker = "X" * (200 * 1024)  # ~200 KiB — well over the old 64 KiB default
    big_line = json.dumps({"type": "system", "subtype": "big", "marker": marker})
    result_line = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "num_turns": 1, "result": "OK", "total_cost_usd": 0.01,
        "usage": {"input_tokens": 1, "output_tokens": 1,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    })
    claude = _fake_claude_lines(tmp, [big_line, result_line], exit_code=0)
    ex = mod.RunExecutor(store, claude_path=claude)
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    run = await ex.wait_for(run_id)

    assert run["status"] == store.RunStatus.SUCCEEDED
    events = store.get_events(run_id, limit=100)
    assert [e["kind"] for e in events] == ["system", "result"]
    assert marker in events[0]["payload"]
    assert run["result_text"] == "OK"


@pytest.mark.asyncio
async def test_oversized_line_is_skipped_and_next_line_still_parsed(env, monkeypatch, caplog):
    """A raised ceiling is not a guarantee. A line bigger than even the raised
    limit must be caught, discarded (stream stays aligned to the next JSON
    object), and logged — while the NEXT well-formed line still parses and
    the run still reaches a terminal state."""
    store, mod, tmp = env
    # A small limit keeps this test fast without changing the property being
    # tested: the same code path fires at 64 MiB, it would just take longer
    # to reproduce.
    monkeypatch.setattr(mod.claude_env, "STREAM_LINE_LIMIT", 64 * 1024)
    oversized = "J" * (300 * 1024)  # not even valid JSON — must never reach the parser
    result_line = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "num_turns": 1, "result": "OK", "total_cost_usd": 0.0,
        "usage": {"input_tokens": 1, "output_tokens": 1,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    })
    claude = _fake_claude_lines(tmp, [oversized, result_line], exit_code=0)
    ex = mod.RunExecutor(store, claude_path=claude)
    with caplog.at_level("WARNING"):
        run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
        run = await ex.wait_for(run_id)

    assert run["status"] == store.RunStatus.SUCCEEDED
    events = store.get_events(run_id, limit=100)
    assert [e["kind"] for e in events] == ["result"]
    assert run["result_text"] == "OK"
    assert any("oversized" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_oversized_line_run_still_reaches_terminal_state_on_nonzero_exit(env, monkeypatch):
    """The dropped-event path must not itself get a run stuck in `running`:
    even when the oversized line is the ONLY thing on stdout and the process
    then exits non-zero, the run still finishes terminal."""
    store, mod, tmp = env
    monkeypatch.setattr(mod.claude_env, "STREAM_LINE_LIMIT", 64 * 1024)
    oversized = "J" * (300 * 1024)
    claude = _fake_claude_lines(tmp, [oversized], exit_code=1)
    ex = mod.RunExecutor(store, claude_path=claude)
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    run = await ex.wait_for(run_id)

    assert run["status"] in store.RunStatus.TERMINAL
    assert run["status"] == store.RunStatus.FAILED
    # The oversized line must be *skipped*, not raised as an unhandled
    # exception out of the reader: the run should fail the ordinary way
    # (non-zero exit), not via the driver's catch-all exception handler.
    assert run["error"] == "exit code 1"


# ==========================================================================
# Invariant 1, for real: a run ALWAYS reaches a terminal state.
#
# The reviewer's reproduction: a child that writes one valid line, CLOSES
# STDOUT, then sleeps. `_consume` returns on EOF; `await proc.wait()` then had
# no bound at all, so the driver parked forever, the run stayed `running`, and
# one of three concurrency permits was gone for the life of the server.
#
# Worse, `_procs.pop(run_id)` ran in the `finally` *before* that wait, so
# `cancel()` took the "already gone" branch: it wrote CANCELLED, returned
# True, and never signalled anything. The real `claude` kept running.
# ==========================================================================


def _eof_then_hang(tmp_path: Path, name: str = "eof_hang.py",
                   sleep_sec: float = 3600) -> str:
    """One valid line, then stdout is CLOSED, then the process sleeps.

    This is the reviewer's fake `claude`, verbatim in behaviour: EOF on
    stdout while the process is very much alive.
    """
    return _script(tmp_path, name,
                   "import os\n"
                   "sys.stdout.write(open(%r).readline())\n"
                   "sys.stdout.flush()\n"
                   "os.close(1)\n"
                   "if os.name == 'nt':\n"
                   "    import ctypes\n"
                   "    k = ctypes.WinDLL('kernel32', use_last_error=True)\n"
                   "    k.GetStdHandle.restype = ctypes.c_void_p\n"
                   "    k.CloseHandle.argtypes = [ctypes.c_void_p]\n"
                   "    k.CloseHandle(k.GetStdHandle(-11))\n"
                   "time.sleep(%r)\n" % (str(FIXTURE), sleep_sec))


async def _await_pid(store, run_id: str, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = store.get_run(run_id)
        if run and run["pid"]:
            return int(run["pid"])
        await asyncio.sleep(0.01)
    raise AssertionError(f"run {run_id} never recorded a pid")


def _alive(pid: int) -> bool:
    # Not `os.kill(pid, 0)`: on Windows that is a Ctrl+C, not a probe.
    return procs.pid_alive(pid)


@pytest.mark.asyncio
async def test_child_that_closes_stdout_and_hangs_reaches_terminal(env, tmp_path):
    """EOF on stdout must not mean an unbounded wait. The driver gets a grace
    period for the child to exit on its own, then kills it."""
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_eof_then_hang(tmp),
                         grace_sec=1.0, eof_grace_sec=0.5)
    run_id = await ex.spawn("hang after eof", "proj", str(tmp), "api")
    pid = await _await_pid(store, run_id)

    run = await asyncio.wait_for(ex.wait_for(run_id, timeout=20), timeout=25)
    assert run["status"] in store.RunStatus.TERMINAL, run
    assert not _alive(pid), "the hung child was left running"


@pytest.mark.asyncio
async def test_post_eof_hang_does_not_leak_the_concurrency_permit(env, tmp_path):
    """One hung child must not permanently shrink capacity."""
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_eof_then_hang(tmp),
                         max_concurrent=1, grace_sec=1.0, eof_grace_sec=0.5)
    hung = await ex.spawn("hang after eof", "proj", str(tmp), "api")
    await asyncio.wait_for(ex.wait_for(hung, timeout=20), timeout=25)

    ex._claude_path = _fake_claude(tmp, FIXTURE)
    good = await ex.spawn("a normal run", "proj", str(tmp), "api")
    run = await asyncio.wait_for(ex.wait_for(good, timeout=20), timeout=25)
    assert run["status"] == store.RunStatus.SUCCEEDED, run


@pytest.mark.asyncio
async def test_cancel_during_the_post_eof_wait_actually_kills(env, tmp_path):
    """`cancel()` must never return True without signalling a live process.

    In the post-EOF window the process is alive and `_procs` used to be
    empty, so cancel() wrote CANCELLED and killed nothing.
    """
    store, mod, tmp = env
    # A long EOF grace so the driver is definitely still waiting when we cancel.
    ex = mod.RunExecutor(store, claude_path=_eof_then_hang(tmp),
                         grace_sec=2.0, eof_grace_sec=60.0)
    run_id = await ex.spawn("hang after eof", "proj", str(tmp), "api")
    pid = await _await_pid(store, run_id)

    # Wait until the one line has been consumed and stdout hit EOF.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and store.count_events(run_id) < 1:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.3)
    assert _alive(pid), "the child died before the window under test"

    assert await ex.cancel(run_id) is True
    assert not _alive(pid), "cancel() returned True but killed nothing"

    run = await asyncio.wait_for(ex.wait_for(run_id, timeout=20), timeout=25)
    assert run["status"] == store.RunStatus.CANCELLED, run


# -- a default timeout: "no timeout" is not an option for an unattended run --

@pytest.mark.asyncio
async def test_a_caller_that_passes_no_timeout_still_gets_one(env, tmp_path):
    """`spawn_run` and `start_build` both omit `timeout_sec`, and
    `RunRequest.timeout_sec` defaults to 0. That left `_drive` unbounded end
    to end for every production caller."""
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_slow_claude(tmp, "slow_default.py"),
                         grace_sec=1.0, default_timeout_sec=1.0)
    run_id = await ex.spawn("hang", "proj", str(tmp), "api")   # no timeout_sec
    run = await asyncio.wait_for(ex.wait_for(run_id, timeout=25), timeout=30)
    assert run["status"] == store.RunStatus.TIMED_OUT, run


@pytest.mark.asyncio
async def test_an_explicit_zero_timeout_also_gets_the_default(env, tmp_path):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_slow_claude(tmp, "slow_zero.py"),
                         grace_sec=1.0, default_timeout_sec=1.0)
    run_id = await ex.spawn("hang", "proj", str(tmp), "api", timeout_sec=0)
    run = await asyncio.wait_for(ex.wait_for(run_id, timeout=25), timeout=30)
    assert run["status"] == store.RunStatus.TIMED_OUT, run


def test_the_module_default_timeout_is_finite_and_generous(env):
    store, mod, tmp = env
    assert 0 < mod._DEFAULT_TIMEOUT_SEC < 24 * 3600
    # A `start_build` is meant to run for hours; the bound must not cut it off.
    assert mod._DEFAULT_TIMEOUT_SEC >= 4 * 3600


def test_the_default_timeout_is_overridable_by_env(env, monkeypatch):
    store, mod, tmp = env
    monkeypatch.setenv("JARVIS_RUN_TIMEOUT_SEC", "42")
    assert mod.RunExecutor(store, claude_path="claude")._default_timeout == 42


# ==========================================================================
# Finding 3: `spawn()` could leave a run QUEUED forever.
#
# `update_run(requested_model=…)` sits between `create_run` and
# `create_task(_drive)`. It is a synchronous SQLite write, and SQLite writes
# raise — "database is locked", a full disk. When it did, the row existed
# with no driver behind it and no path to a terminal state: exactly the case
# CLAUDE.md warns callers about, with RunExecutor itself as the offender.
# ==========================================================================


@pytest.mark.asyncio
async def test_a_store_failure_inside_spawn_still_fails_the_run(env, monkeypatch):
    import sqlite3
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_fake_claude(tmp, FIXTURE))

    real = store.update_run

    def boom(run_id, **fields):
        if "requested_model" in fields:
            raise sqlite3.OperationalError("database is locked")
        return real(run_id, **fields)

    monkeypatch.setattr(store, "update_run", boom)

    with pytest.raises(sqlite3.OperationalError):
        await ex.spawn("do a thing", "proj", str(tmp), "api")

    rows = store.list_runs(limit=5)
    assert rows, "spawn created no row at all"
    run = rows[0]
    assert run["status"] in store.RunStatus.TERMINAL, run
    assert run["status"] == store.RunStatus.FAILED, run
    assert "database is locked" in run["error"], run
    assert run["ended_at"] is not None


@pytest.mark.asyncio
async def test_a_task_that_cannot_be_created_still_fails_the_run(env, monkeypatch):
    """Anything after `create_run` — not just the store write."""
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_fake_claude(tmp, FIXTURE))
    monkeypatch.setattr(mod.asyncio, "create_task",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("no running event loop")))

    with pytest.raises(RuntimeError):
        await ex.spawn("do a thing", "proj", str(tmp), "api")

    run = store.list_runs(limit=5)[0]
    assert run["status"] == store.RunStatus.FAILED, run
    assert "no running event loop" in run["error"], run


@pytest.mark.asyncio
async def test_failing_an_undriven_run_never_raises_over_the_original(env, monkeypatch):
    """If the store is the thing that is broken, the recovery write fails too.
    The caller must still see the ORIGINAL exception, not the second one."""
    import sqlite3
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_fake_claude(tmp, FIXTURE))

    def always_boom(run_id, **fields):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "update_run", always_boom)

    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        await ex.spawn("do a thing", "proj", str(tmp), "api")


# ==========================================================================
# Finding 4: `is_error` was extracted and thrown away.
#
# `stream_parser.extract_result_metrics` has always computed it; `_consume`
# stored every other field and dropped this one, and `_drive` decided the
# run's fate on `returncode` alone. brain.py treats the identical field as
# fatal, with a comment recording that the CLI emits `subtype: "success"`
# with `is_error: true` for an auth failure — and the CLI exits 0 for it. So
# a run that never ran was recorded `succeeded`, and `assess_outcome` cannot
# catch it: a run that wrote files and THEN errored looks like work done.
# ==========================================================================


def _result_line(**over) -> str:
    event = {"type": "result", "subtype": "success", "is_error": False,
             "num_turns": 1, "result": "OK", "total_cost_usd": 0.01,
             "usage": {"input_tokens": 1, "output_tokens": 1,
                       "cache_read_input_tokens": 0,
                       "cache_creation_input_tokens": 0}}
    event.update(over)
    return json.dumps(event)


@pytest.mark.asyncio
async def test_is_error_result_with_exit_zero_is_not_succeeded(env):
    store, mod, tmp = env
    lines = [
        json.dumps({"type": "system", "subtype": "init", "model": "sonnet"}),
        _result_line(is_error=True,
                     result="API Error: 400 anthropic-workspace-id is required"),
    ]
    ex = mod.RunExecutor(store, claude_path=_fake_claude_lines(tmp, lines,
                                                               exit_code=0))
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    run = await ex.wait_for(run_id, timeout=20)
    assert run["status"] == store.RunStatus.FAILED, run
    assert run["exit_code"] == 0, run
    assert "anthropic-workspace-id" in run["error"], run


@pytest.mark.asyncio
async def test_is_error_is_stored_on_the_run(env):
    store, mod, tmp = env
    lines = [_result_line(is_error=True, result="nope")]
    ex = mod.RunExecutor(store, claude_path=_fake_claude_lines(tmp, lines))
    run = await ex.wait_for(await ex.spawn("x", "proj", str(tmp), "api"),
                            timeout=20)
    assert run["is_error"] == 1, run


@pytest.mark.asyncio
async def test_a_clean_result_is_not_marked_as_an_error(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_fake_claude_lines(
        tmp, [_result_line()], exit_code=0))
    run = await ex.wait_for(await ex.spawn("x", "proj", str(tmp), "api"),
                            timeout=20)
    assert run["status"] == store.RunStatus.SUCCEEDED, run
    assert run["is_error"] == 0, run


@pytest.mark.asyncio
async def test_cancelling_beats_is_error(env):
    """A cancelled run stays CANCELLED even if its last result said is_error:
    the user's intent outranks the CLI's verdict, same as with exit -15."""
    store, mod, tmp = env
    lines = [_result_line(is_error=True, result="nope")]
    ex = mod.RunExecutor(store, claude_path=_fake_claude_lines(tmp, lines))
    run_id = await ex.spawn("x", "proj", str(tmp), "api")
    ex._cancelling.add(run_id)
    run = await ex.wait_for(run_id, timeout=20)
    assert run["status"] == store.RunStatus.CANCELLED, run


# ==========================================================================
# Finding 5: `totals` was declared "so the dashboard can watch them climb"
# and then never touched again, and `stream_parser.extract_assistant_usage`
# had zero callers. Two docstrings asserted live token counting that did not
# exist: the counters moved exactly once, at the terminal `result`.
#
# Wired rather than deleted. `detail.ts` already renders
# `run.input_tokens / output_tokens / cache_read_tokens` off the run row, and
# `run_updated` already carries the whole row, so accumulating into the same
# columns makes the numbers climb with no frontend change at all. The
# terminal `result` then overwrites the running estimate with the CLI's own
# authoritative totals.
# ==========================================================================


def _assistant_line(inp: int, out: int, cache_read: int = 0,
                    cache_creation: int = 0, text: str = "working") -> str:
    return json.dumps({
        "type": "assistant",
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "usage": {"input_tokens": inp, "output_tokens": out,
                              "cache_read_input_tokens": cache_read,
                              "cache_creation_input_tokens": cache_creation}},
    })


@pytest.mark.asyncio
async def test_tokens_climb_before_the_result_event(env):
    store, mod, tmp = env
    lines = [
        json.dumps({"type": "system", "subtype": "init", "model": "sonnet"}),
        _assistant_line(3, 5, cache_read=100, cache_creation=7),
        _assistant_line(4, 6, cache_read=200, cache_creation=8),
        _result_line(usage={"input_tokens": 99, "output_tokens": 98,
                            "cache_read_input_tokens": 97,
                            "cache_creation_input_tokens": 96}),
    ]
    ex = mod.RunExecutor(store, claude_path=_fake_claude_lines(tmp, lines))
    seen: list[dict] = []
    ex.subscribe(seen.append)
    run_id = await ex.spawn("x", "proj", str(tmp), "api")
    await ex.wait_for(run_id, timeout=20)

    climbed = [m["run"] for m in seen
               if m["type"] == "run_updated" and m["run"]["output_tokens"]]
    assert climbed, f"tokens never climbed before the result: {seen}"
    # Cumulative across assistant turns, not last-write-wins.
    assert [r["output_tokens"] for r in climbed][:2] == [5, 11], climbed
    assert [r["input_tokens"] for r in climbed][:2] == [3, 7], climbed
    assert [r["cache_read_tokens"] for r in climbed][:2] == [100, 300], climbed
    assert [r["cache_creation_tokens"] for r in climbed][:2] == [7, 15], climbed

    # And the terminal result still wins: it is the CLI's own total, never
    # an estimate summed by us.
    final = store.get_run(run_id)
    assert (final["input_tokens"], final["output_tokens"]) == (99, 98), final
    assert final["cache_read_tokens"] == 97 and final["cache_creation_tokens"] == 96


@pytest.mark.asyncio
async def test_a_turn_that_reports_no_usage_publishes_nothing(env):
    """A `run_updated` per assistant turn regardless would be one DB write and
    one broadcast per turn for no change at all."""
    store, mod, tmp = env
    lines = [
        _assistant_line(0, 0),
        _assistant_line(0, 0),
        _result_line(),
    ]
    ex = mod.RunExecutor(store, claude_path=_fake_claude_lines(tmp, lines))
    seen: list[dict] = []
    ex.subscribe(seen.append)
    await ex.wait_for(await ex.spawn("x", "proj", str(tmp), "api"), timeout=20)
    updates = [m for m in seen if m["type"] == "run_updated"]
    # Only the one from `result`.
    assert len(updates) == 1, updates


def test_extract_assistant_usage_has_a_caller():
    """The claim in its docstring — that the executor accumulates these — has
    to be true. Deleting the call must break this."""
    import inspect
    import run_executor as mod
    assert "extract_assistant_usage" in inspect.getsource(mod)


# ==========================================================================
# Finding 6: synchronous SQLite on the event loop.
#
# `_finish` did three sync store calls on the loop thread while `_consume`
# wrote the same database from a worker thread. With sqlite's 5s busy
# timeout, `_finish` could stall the voice path — and every OTHER store call
# in this module was already `to_thread`'d, so this was an inconsistency as
# much as a bug.
#
# These tests measure the thing that matters: whether the loop keeps turning.
# A slow store is simulated so a real lock does not have to be provoked.
# ==========================================================================


class _LoopTicker:
    """Whether the event loop kept turning: the LONGEST GAP between two of
    its turns, in `max_gap`.

    It used to count turns of `asyncio.sleep(0.005)` and demand more than 30
    in a 0.4 s write. On Windows the loop's timers fire at the system clock's
    15.6 ms resolution, so a loop that never blocked got about 25, and all
    three tests below failed on every Windows run. The property they state
    is "the loop was not blocked through the write", and the longest stall
    between turns IS that property on any clock: blocked, it stalls for the
    whole write; free, for about one timer tick. `ticks` is kept for the
    failure message.

    The gap still OPEN at exit counts too. A terminal write is the last
    thing a run does, so a block there ends the window, and the ticker is
    cancelled before it wakes to close that gap — which is how the
    terminal-write test passed with every store call moved onto the loop.
    """

    def __init__(self):
        self.ticks = 0
        self.max_gap = 0.0
        self._last = 0.0
        self._task = None

    async def __aenter__(self):
        async def tick():
            self._last = time.monotonic()
            while True:
                self.ticks += 1
                await asyncio.sleep(0.005)
                now = time.monotonic()
                self.max_gap = max(self.max_gap, now - self._last)
                self._last = now
        self._task = asyncio.create_task(tick())
        await asyncio.sleep(0.05)
        self.ticks = 0
        self.max_gap = 0.0
        return self

    async def __aexit__(self, *exc):
        self.max_gap = max(self.max_gap, time.monotonic() - self._last)
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_the_ticker_sees_a_loop_that_is_blocked():
    """The instrument first: a measure that cannot see a blocked loop would
    pass the three tests below vacuously."""
    async with _LoopTicker() as ticker:
        time.sleep(0.4)                  # the loop thread, blocked outright
        await asyncio.sleep(0.05)
    assert ticker.max_gap >= 0.35, ticker.max_gap


@pytest.mark.asyncio
async def test_the_ticker_sees_a_block_at_the_very_end_of_its_window():
    """A terminal write is the LAST thing a run does, so a block there ends
    the window: the ticker is cancelled before it ever wakes to measure the
    gap. Measured by mutation — every store call moved onto the loop thread —
    and the terminal-write test still passed."""
    async with _LoopTicker() as ticker:
        await asyncio.sleep(0.02)
        time.sleep(0.4)
    assert ticker.max_gap >= 0.35, ticker.max_gap


@pytest.mark.asyncio
async def test_the_ticker_does_not_mistake_a_coarse_clock_for_a_block():
    """Windows fires asyncio timers at the system clock's 15.6 ms, so a loop
    that never blocked got ~25 turns of `sleep(0.005)` in 0.4 s — under the
    old bar of 30. Measured on every Windows run, 2026-09-28."""
    async with _LoopTicker() as ticker:
        await asyncio.sleep(0.4)
    assert ticker.max_gap < 0.2, ticker.max_gap


@pytest.mark.asyncio
async def test_the_terminal_write_does_not_freeze_the_loop(env, monkeypatch):
    """A slow `_finish` used to block the loop thread outright. The voice
    path shares that thread."""
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_fake_claude(tmp, FIXTURE))
    run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
    await ex.wait_for(run_id, timeout=20)

    # A second run whose terminal write is slow. `transition_run` is the
    # write: since 45b7aba the status transitions are claimed through it, and
    # this test slowed `update_run` — which a transition no longer calls — so
    # it slowed nothing and could not fail. `slowed` is the proof it bites.
    real_transition = store.transition_run
    slowed = []

    def slow_transition(rid, status, **fields):
        if status in store.RunStatus.TERMINAL:
            slowed.append(status)
            time.sleep(0.4)
        return real_transition(rid, status, **fields)

    monkeypatch.setattr(store, "transition_run", slow_transition)

    async with _LoopTicker() as ticker:
        second = await ex.spawn("again", "proj", str(tmp), "api")
        await ex.wait_for(second, timeout=20)

    assert store.get_run(second)["status"] == store.RunStatus.SUCCEEDED
    assert slowed, "the slow write never happened, so this measured nothing"
    # Blocked, the loop would stall for the whole 0.4s write; turning, it
    # stalls for about one timer tick. Half the write separates the two.
    assert ticker.max_gap < 0.2, (
        f"the loop stalled {ticker.max_gap:.3f}s during a 0.4s store write "
        f"({ticker.ticks} turns) — it was blocked on the loop thread")


@pytest.mark.asyncio
async def test_the_running_transition_does_not_freeze_the_loop(env, monkeypatch):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_fake_claude(tmp, FIXTURE))
    # Slowed where the transition is written — see the test above.
    real_transition = store.transition_run
    slowed = []

    def slow_transition(rid, status, **fields):
        if status == store.RunStatus.RUNNING:
            slowed.append(status)
            time.sleep(0.4)
        return real_transition(rid, status, **fields)

    monkeypatch.setattr(store, "transition_run", slow_transition)

    async with _LoopTicker() as ticker:
        run_id = await ex.spawn("do a thing", "proj", str(tmp), "api")
        await ex.wait_for(run_id, timeout=20)

    assert store.get_run(run_id)["status"] == store.RunStatus.SUCCEEDED
    assert slowed, "the slow write never happened, so this measured nothing"
    assert ticker.max_gap < 0.2, (
        f"the loop stalled {ticker.max_gap:.3f}s during a 0.4s store write "
        f"({ticker.ticks} turns)")


@pytest.mark.asyncio
async def test_cancelling_does_not_freeze_the_loop(env, monkeypatch):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_slow_claude(tmp, "slow_cancel.py"),
                         grace_sec=2.0)
    run_id = await ex.spawn("hang", "proj", str(tmp), "api")
    await _await_status(store, run_id, store.RunStatus.RUNNING)

    real_get = store.get_run
    slow = True

    def slow_get(rid):
        if slow:
            time.sleep(0.4)
        return real_get(rid)

    monkeypatch.setattr(store, "get_run", slow_get)

    async with _LoopTicker() as ticker:
        assert await ex.cancel(run_id) is True

    slow = False
    assert store.get_run(run_id)["status"] == store.RunStatus.CANCELLED
    assert ticker.max_gap < 0.2, (
        f"the loop stalled {ticker.max_gap:.3f}s during cancel() "
        f"({ticker.ticks} turns)")


@pytest.mark.asyncio
async def test_a_multi_megabyte_event_is_not_stored_verbatim(env):
    """`run_events.payload` is permanent. STREAM_LINE_LIMIT is 64 MiB, so one
    line could put several MiB into jarvis.db for good, and a chatty build
    emits thousands of events."""
    store, mod, tmp = env
    import stream_parser
    huge = json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1",
         "content": "z" * (3 * 1024 * 1024)}]}})
    ex = mod.RunExecutor(store, claude_path=_fake_claude_lines(
        tmp, [huge, _result_line()]))
    run_id = await ex.spawn("x", "proj", str(tmp), "api")
    run = await ex.wait_for(run_id, timeout=30)
    assert run["status"] == store.RunStatus.SUCCEEDED, run

    events = store.get_events(run_id, limit=10)
    assert len(events) == 2
    stored = events[0]["payload"]
    assert len(stored) <= stream_parser.PAYLOAD_MAX_CHARS, len(stored)
    # Still a usable event, not a blob of text.
    parsed = stream_parser.parse_line(stored)
    assert parsed is not None and parsed["type"] == "user"
    assert parsed["message"]["content"][0]["tool_use_id"] == "toolu_1"


@pytest.mark.asyncio
async def test_an_ordinary_event_is_still_stored_verbatim(env):
    store, mod, tmp = env
    line = json.dumps({"type": "assistant", "message": {
        "role": "assistant", "content": [
            {"type": "tool_use", "id": "t", "name": "Write",
             "input": {"file_path": "/tmp/x", "content": "hello"}}]}})
    ex = mod.RunExecutor(store, claude_path=_fake_claude_lines(
        tmp, [line, _result_line()]))
    run_id = await ex.spawn("x", "proj", str(tmp), "api")
    await ex.wait_for(run_id, timeout=20)
    assert store.get_events(run_id, limit=10)[0]["payload"] == line


# ==========================================================================
# The EOF grace only fires when the reader actually sees EOF.
#
# Measured at the asyncio level, one child shape per row:
#
#   os.close(1)          -> EOF immediately          grace fires        OK
#   sys.stdout.close()   -> NO EOF, readline blocks  grace never fires  WEDGED
#   write then linger    -> NO EOF, readline blocks  grace never fires  WEDGED
#   exit, grandchild
#     holding fd 1       -> NO EOF, readline blocks  grace never fires  WEDGED
#   SIGSTOP itself       -> NO EOF, readline blocks  grace never fires  WEDGED
#
# `_EOF_EXIT_GRACE_SEC` is applied *after* `_consume` returns, and `_consume`
# only returns when readline() reports EOF. In four of those five shapes it
# never does, so `_drive` parks in `wait_for(reader, timeout=timeout_sec)`
# and the only bound left is the six-hour wall clock. The run stays
# `running` and holds one of three permits; three of them wedge the pipeline
# for the rest of the day.
#
# Why `sys.stdout.close()` is not EOF, since it is the surprising one:
# CPython builds the raw FileIO behind `sys.stdout` with `closefd=False`
# (pylifecycle's create_stdio), precisely so that closing the Python object
# cannot take fd 1 away from the C runtime. So `sys.stdout.close()` tears
# down the buffered wrapper and leaves the pipe's write end open — it is not
# the `os.close(1)` case at all, it is the "stopped talking" case.
#
# Two bounds close it: an idle timeout on the reader, and a `returncode`
# poll inside the read loop so a child that has *exited* is reaped even
# though a descendant still holds the pipe.
# ==========================================================================


def _write_lines(tmp_path: Path, name: str, lines: list[str]) -> Path:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# Suspending a process is a POSIX signal; Windows has no SIGSTOP, so that one
# way of going quiet cannot be staged there. The other three are portable.
_SIGSTOP = pytest.param("sigstop", marks=pytest.mark.skipif(
    sys.platform == "win32", reason="SIGSTOP does not exist on Windows"))


def _stops_talking(tmp_path: Path, how: str, sleep_sec: float = 120) -> str:
    """A stand-in `claude` that emits one line and then stops, `how` ways."""
    bodies = {
        # The one shape the existing EOF grace already covers.
        "close_fd": "import os\nos.close(1)\ntime.sleep(%r)\n" % sleep_sec,
        # Looks identical in the source. Is not: fd 1 stays open.
        "close_py": "sys.stdout.close()\ntime.sleep(%r)\n" % sleep_sec,
        # A build that wandered off, or a tool call that never returns.
        "silent": "time.sleep(%r)\n" % sleep_sec,
        # Suspended: alive, holding the pipe, will never write again.
        "sigstop": ("import os, signal\n"
                    "os.kill(os.getpid(), signal.SIGSTOP)\n"
                    "time.sleep(%r)\n" % sleep_sec),
    }
    return _script(tmp_path, f"stops_{how}.py",
                   "sys.stdout.write(open(%r).readline())\n"
                   "sys.stdout.flush()\n" % str(FIXTURE) + bodies[how])


@pytest.mark.parametrize("how", ["close_fd", "close_py", "silent", _SIGSTOP])
@pytest.mark.asyncio
async def test_every_way_of_going_quiet_still_reaches_a_terminal_state(
        env, how):
    """Not just the one shape the fixer happened to write."""
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_stops_talking(tmp, how),
                         grace_sec=1.0, eof_grace_sec=0.5,
                         idle_sec=1.0, poll_sec=0.1)
    run_id = await ex.spawn("go quiet", "proj", str(tmp), "api")
    pid = await _await_pid(store, run_id)

    run = await asyncio.wait_for(ex.wait_for(run_id, timeout=20), timeout=25)
    assert run["status"] in store.RunStatus.TERMINAL, run
    assert run["status"] != store.RunStatus.SUCCEEDED, (
        f"a child that went quiet by {how} was recorded as a success: {run}")
    assert not _alive(pid), f"the {how} child was left running"


@pytest.mark.parametrize("how", ["close_fd", "close_py", "silent", _SIGSTOP])
@pytest.mark.asyncio
async def test_no_way_of_going_quiet_leaks_the_concurrency_permit(env, how):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_stops_talking(tmp, how),
                         max_concurrent=1, grace_sec=1.0, eof_grace_sec=0.5,
                         idle_sec=1.0, poll_sec=0.1)
    stuck = await ex.spawn("go quiet", "proj", str(tmp), "api")
    await asyncio.wait_for(ex.wait_for(stuck, timeout=20), timeout=25)

    ex._claude_path = _fake_claude(tmp, FIXTURE)
    good = await ex.spawn("a normal run", "proj", str(tmp), "api")
    run = await asyncio.wait_for(ex.wait_for(good, timeout=20), timeout=25)
    assert run["status"] == store.RunStatus.SUCCEEDED, run


@pytest.mark.asyncio
async def test_the_idle_bound_says_what_happened(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path=_stops_talking(tmp, "silent"),
                         grace_sec=1.0, idle_sec=1.0, poll_sec=0.1)
    run_id = await ex.spawn("go quiet", "proj", str(tmp), "api")
    run = await asyncio.wait_for(ex.wait_for(run_id, timeout=20), timeout=25)
    assert run["status"] == store.RunStatus.TIMED_OUT, run
    assert "no output" in run["error"], run


# -- a child that has EXITED but whose grandchild still holds the pipe -----


def _exits_leaving_a_grandchild(tmp_path: Path, name: str,
                                inherit_stderr: bool) -> str:
    """The auditor's shape: `claude` exits 0, a descendant keeps fd 1.

    The real CLI spawns MCP children; any one of them that outlives it and
    inherited stdout produces exactly this. The process is *gone* — its exit
    status is known and it is 0 — but the pipe never reports EOF.
    """
    stderr = "" if inherit_stderr else ", stderr=subprocess.DEVNULL"
    return _script(
        tmp_path, name,
        "import subprocess\n"
        "sys.stdout.write(open(%r).readline())\n"
        "sys.stdout.write(%r + '\\n')\n"
        "sys.stdout.flush()\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)']%s)\n"
        "sys.exit(0)\n" % (str(FIXTURE), _result_line(), stderr))


@pytest.mark.asyncio
async def test_a_child_that_exited_is_reaped_even_though_the_pipe_is_held(env):
    """It exited 0. That is a success, and it must be recorded promptly —
    not after the idle bound, and certainly not after six hours."""
    store, mod, tmp = env
    ex = mod.RunExecutor(store,
                         claude_path=_exits_leaving_a_grandchild(
                             tmp, "grandchild_stdout.py", inherit_stderr=False),
                         grace_sec=1.0, eof_grace_sec=0.5,
                         idle_sec=30.0, poll_sec=0.1)
    started = time.monotonic()
    run_id = await ex.spawn("leave a grandchild", "proj", str(tmp), "api")
    run = await asyncio.wait_for(ex.wait_for(run_id, timeout=25), timeout=30)
    elapsed = time.monotonic() - started

    assert run["status"] == store.RunStatus.SUCCEEDED, run
    assert run["exit_code"] == 0, run
    assert elapsed < 10, (
        f"the exited child took {elapsed:.1f}s to reap; the idle bound was "
        f"30s, so nothing noticed it had exited")


@pytest.mark.asyncio
async def test_a_grandchild_holding_stderr_too_is_still_bounded(env):
    """Both pipes held. The stderr drain already has its own ceiling; the
    stdout side is what had none."""
    store, mod, tmp = env
    ex = mod.RunExecutor(store,
                         claude_path=_exits_leaving_a_grandchild(
                             tmp, "grandchild_both.py", inherit_stderr=True),
                         grace_sec=1.0, eof_grace_sec=0.5,
                         idle_sec=30.0, poll_sec=0.1)
    started = time.monotonic()
    run_id = await ex.spawn("leave a grandchild", "proj", str(tmp), "api")
    run = await asyncio.wait_for(ex.wait_for(run_id, timeout=25), timeout=30)
    elapsed = time.monotonic() - started

    assert run["status"] == store.RunStatus.SUCCEEDED, run
    # Under the grandchild's own 10s lifetime, so this cannot pass by
    # accidentally outliving it: detection (~2 polls) plus the stderr
    # drain's own 5s ceiling.
    assert elapsed < 9, f"took {elapsed:.1f}s"


# -- and the other direction: a slow build is not a stalled one -----------


@pytest.mark.asyncio
async def test_a_run_that_is_talking_slowly_is_never_cut_off(env):
    """The idle timer must reset on every line. A build that emits an event
    every 0.3s for longer than the whole idle budget is working, not stuck —
    downgrading it would be a worse bug than the one being fixed."""
    store, mod, tmp = env
    claude = _script(
        tmp, "slow_but_talking.py",
        "line = open(%r).readline()\n"
        "for _ in range(6):\n"
        "    sys.stdout.write(line)\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.3)\n"
        "sys.stdout.write(%r + '\\n')\n"
        "sys.stdout.flush()\n"
        "sys.exit(0)\n" % (str(FIXTURE), _result_line()))
    ex = mod.RunExecutor(store, claude_path=claude, grace_sec=1.0,
                         idle_sec=1.0, poll_sec=0.1)
    run_id = await ex.spawn("work slowly", "proj", str(tmp), "api")
    run = await asyncio.wait_for(ex.wait_for(run_id, timeout=25), timeout=30)
    assert run["status"] == store.RunStatus.SUCCEEDED, run
    assert store.count_events(run_id) == 7, store.get_events(run_id, limit=20)


@pytest.mark.asyncio
async def test_the_poll_loop_does_not_drop_a_line(env):
    """`readline()` is waited on, never cancelled and restarted: a line that
    arrives in the same instant the poll expires must not be lost."""
    store, mod, tmp = env
    lines = [json.dumps({"type": "system", "subtype": "init",
                         "model": "sonnet"})] * 40 + [_result_line()]
    payload = _write_lines(tmp, "trickle.txt", lines)
    claude = _script(
        tmp, "trickle.py",
        "for line in open(%r).read().splitlines():\n"
        "    sys.stdout.write(line + '\\n')\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.02)\n"
        "sys.exit(0)\n" % str(payload))
    ex = mod.RunExecutor(store, claude_path=claude, grace_sec=1.0,
                         idle_sec=5.0, poll_sec=0.02)
    run_id = await ex.spawn("trickle", "proj", str(tmp), "api")
    run = await asyncio.wait_for(ex.wait_for(run_id, timeout=25), timeout=30)
    assert run["status"] == store.RunStatus.SUCCEEDED, run
    assert store.count_events(run_id) == len(lines), (
        f"{store.count_events(run_id)} of {len(lines)} events survived the "
        f"poll loop")


# -- the value of the bound itself ----------------------------------------


def test_the_idle_bound_is_generous_enough_for_a_real_build(env):
    """A single Bash tool call is allowed ten minutes by the CLI, and it
    emits nothing at all while it runs. Add a long thinking turn and an API
    retry backoff either side of it and the longest legitimate silence is
    still well inside this."""
    store, mod, tmp = env
    assert mod._IDLE_OUTPUT_SEC >= 20 * 60
    assert mod._IDLE_OUTPUT_SEC < mod._DEFAULT_TIMEOUT_SEC


def test_the_idle_bound_is_overridable_by_env(env, monkeypatch):
    store, mod, tmp = env
    monkeypatch.setenv("JARVIS_RUN_IDLE_SEC", "90")
    ex = mod.RunExecutor(store, claude_path="claude")
    assert ex._idle_sec == 90.0


@pytest.mark.parametrize("bad", ["0", "-5", "not-a-number"])
def test_the_idle_bound_cannot_be_switched_off(env, monkeypatch, bad):
    store, mod, tmp = env
    monkeypatch.setenv("JARVIS_RUN_IDLE_SEC", bad)
    ex = mod.RunExecutor(store, claude_path="claude")
    assert ex._idle_sec == float(mod._IDLE_OUTPUT_SEC)


# ==========================================================================
# `_terminate` ended in an unbounded `await proc.wait()` after SIGKILL —
# the one wait in the file with no ceiling.
# ==========================================================================


class _NeverReaped:
    """A child whose death is never reported.

    Not a hypothetical: `proc.wait()` resolves when asyncio's child watcher
    sees the process, and anything that reaps the pid first (a stray
    `os.waitpid`, a watcher torn down mid-flight) leaves that future pending
    for good. A real process cannot be made to do this on demand, so the
    bound is tested against the shape rather than the cause.
    """

    pid = 424242
    returncode = None

    def __init__(self):
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    async def wait(self):
        await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_the_wait_after_sigkill_is_bounded(env):
    store, mod, tmp = env
    ex = mod.RunExecutor(store, claude_path="claude", grace_sec=0.2)
    proc = _NeverReaped()
    with pytest.raises(mod._TerminationUnconfirmed, match="Could not confirm termination"):
        await asyncio.wait_for(ex._terminate(proc), timeout=5)
    assert proc.terminated and proc.killed


@pytest.mark.skipif(sys.platform == "win32",
                    reason="terminate() is TerminateProcess on Windows; it cannot be ignored")
@pytest.mark.asyncio
async def test_terminate_still_reaps_a_child_that_ignores_sigterm(env):
    """The bound must not turn `_terminate` into "signal and hope". A real
    child that ignores SIGTERM is still killed and still reaped."""
    store, mod, tmp = env
    claude = _script(
        tmp, "ignores_sigterm.py",
        "import signal\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "sys.stdout.write(open(%r).readline())\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n" % str(FIXTURE))
    ex = mod.RunExecutor(store, claude_path=claude, grace_sec=0.5,
                         idle_sec=30.0, poll_sec=0.1)
    run_id = await ex.spawn("ignore me", "proj", str(tmp), "api")
    pid = await _await_pid(store, run_id)
    await _await_status(store, run_id, store.RunStatus.RUNNING)

    assert await ex.cancel(run_id) is True
    assert not _alive(pid), "the SIGTERM-ignoring child survived cancel()"
    run = await asyncio.wait_for(ex.wait_for(run_id, timeout=20), timeout=25)
    assert run["status"] == store.RunStatus.CANCELLED, run


# ==========================================================================
# A second `result` event overwrote `is_error`.
# ==========================================================================


@pytest.mark.asyncio
async def test_a_later_result_cannot_unset_is_error(env):
    """`{"is_error": true}` then `{"is_error": false}` used to come back
    `succeeded`: the column was written unconditionally on every result, and
    `_drive` reads whatever the last one left. Once true, true."""
    store, mod, tmp = env
    lines = [_result_line(is_error=True, result="API Error: 400 no workspace"),
             _result_line(is_error=False, result="OK")]
    ex = mod.RunExecutor(store, claude_path=_fake_claude_lines(tmp, lines,
                                                               exit_code=0))
    run = await ex.wait_for(await ex.spawn("x", "proj", str(tmp), "api"),
                            timeout=20)
    assert run["is_error"] == 1, run
    assert run["status"] == store.RunStatus.FAILED, run


@pytest.mark.asyncio
async def test_an_error_on_the_second_result_is_still_an_error(env):
    """The other order must keep working too."""
    store, mod, tmp = env
    lines = [_result_line(is_error=False, result="OK"),
             _result_line(is_error=True, result="API Error: overloaded")]
    ex = mod.RunExecutor(store, claude_path=_fake_claude_lines(tmp, lines,
                                                               exit_code=0))
    run = await ex.wait_for(await ex.spawn("x", "proj", str(tmp), "api"),
                            timeout=20)
    assert run["is_error"] == 1, run
    assert run["status"] == store.RunStatus.FAILED, run
