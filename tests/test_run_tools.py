"""`run_status` and `cancel_run` — JARVIS checking on, and stopping, his own work.

A run id is a UUID. Nobody says one out loud and JARVIS never reads one out,
so both tools have to work from what a person would actually say: a project
name, a few words out of the prompt, or "that one" meaning the thing he just
started. Resolution is shared with `_resolve_project_or_explain`'s rule —
ask, never guess — because `cancel_run` kills a process and stopping the
wrong one is not recoverable by apologising.

`run_status` reads and says, and is therefore deliberately NOT an acting
tool: answering "is it done yet" must not depend on who is talking.
"""

import importlib
import time

import pytest


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server as server_module
    importlib.reload(server_module)
    run_store.init_db()
    return server_module, run_store


class _Executor:
    async def shutdown(self):
        pass
    """Records cancellations. Never touches a process."""

    def __init__(self, store, answer=True, boom=False):
        self.store = store
        self.answer = answer
        self.boom = boom
        self.cancelled: list[str] = []

    async def cancel(self, run_id):
        if self.boom:
            raise RuntimeError("no")
        self.cancelled.append(run_id)
        if self.answer:
            self.store.update_run(run_id, status=self.store.RunStatus.CANCELLED,
                                  ended_at=time.time())
        return self.answer

    async def spawn(self, prompt, project_name, project_path, origin,
                    resume_from=None, timeout_sec=0, model=None):
        return self.store.create_run(prompt, project_name, project_path,
                                     origin, resume_from)


def _run(store, project="chitauri", status=None, prompt="fix the redirect",
         origin="voice", **fields):
    run_id = store.create_run(prompt, project, f"/tmp/{project}", origin)
    if status:
        store.update_run(run_id, status=status, **fields)
    return run_id


def _speakable(text):
    """Anything the brain is handed has to be a sentence it can say."""
    assert isinstance(text, str) and text.strip()
    first_line = text.splitlines()[0].rstrip()
    assert first_line.endswith((".", "?", "!")), first_line
    return text


# --- status with nothing to point at -------------------------------------

def test_status_with_no_argument_and_nothing_running(wired):
    server, _store = wired
    out = _speakable(server.tool_run_status({}))
    assert out == "Nothing is running just now, sir."


def test_status_with_no_argument_mentions_the_last_thing_that_ended(wired):
    server, store = wired
    _run(store, status=store.RunStatus.SUCCEEDED, ended_at=time.time() - 60)
    out = _speakable(server.tool_run_status({}))
    assert "Nothing is running" in out
    assert "chitauri" in out and "worked" in out


def test_status_with_no_argument_reports_the_one_that_is_running(wired):
    server, store = wired
    _run(store, status=store.RunStatus.RUNNING, started_at=time.time() - 180)
    out = _speakable(server.tool_run_status({}))
    assert "chitauri" in out and "still going" in out
    assert "about 3 minutes ago" in out, "ages, never timestamps"


def test_status_with_no_argument_summarises_several(wired):
    server, store = wired
    _run(store, project="chitauri", status=store.RunStatus.RUNNING,
         started_at=time.time() - 60)
    _run(store, project="hammer", status=store.RunStatus.RUNNING,
         started_at=time.time() - 10)
    out = _speakable(server.tool_run_status({}))
    assert "chitauri" in out and "hammer" in out
    assert out.startswith("Two "), "small counts are spelled out for speech"


# --- status for one particular run ---------------------------------------

def test_status_for_a_specific_run_by_id(wired):
    server, store = wired
    run_id = _run(store, status=store.RunStatus.RUNNING,
                  started_at=time.time() - 60)
    out = _speakable(server.tool_run_status({"run": run_id}))
    assert "chitauri" in out and "still going" in out


def test_status_by_project_name(wired):
    server, store = wired
    _run(store, project="hammer", status=store.RunStatus.SUCCEEDED,
         ended_at=time.time() - 30)
    _run(store, project="chitauri", status=store.RunStatus.RUNNING,
         started_at=time.time() - 30)
    out = _speakable(server.tool_run_status({"run": "chitauri"}))
    assert "chitauri" in out and "hammer" not in out


def test_status_prefers_the_live_run_over_a_finished_one(wired):
    server, store = wired
    _run(store, status=store.RunStatus.SUCCEEDED, ended_at=time.time() - 900)
    _run(store, status=store.RunStatus.RUNNING, started_at=time.time() - 30)
    out = _speakable(server.tool_run_status({"run": "chitauri"}))
    assert "still going" in out


def test_status_for_a_back_reference_finds_what_spawn_run_started(wired,
                                                                  monkeypatch):
    """"How's that one going" has to work — nobody can say a UUID."""
    server, store = wired
    ex = _Executor(store)
    monkeypatch.setattr(server, "run_executor_instance", ex)
    monkeypatch.setattr(server, "cached_projects",
                        [{"name": "chitauri", "path": "/tmp/chitauri"}])

    import asyncio
    asyncio.run(server.tool_spawn_run({"project": "chitauri",
                                       "prompt": "build it"}))
    store.update_run(server.last_started_run,
                     status=store.RunStatus.RUNNING,
                     started_at=time.time() - 45)

    out = _speakable(server.tool_run_status({"run": "that one"}))
    assert "chitauri" in out and "still going" in out


def test_a_back_reference_with_nothing_started_says_so(wired):
    server, _store = wired
    out = _speakable(server.tool_run_status({"run": "that one"}))
    assert "haven't started" in out


def test_status_for_an_unknown_reference_asks_rather_than_inventing(wired):
    server, store = wired
    _run(store, status=store.RunStatus.RUNNING)
    out = _speakable(server.tool_run_status({"run": "kestrel"}))
    # The asked-for name is not echoed: it is the brain's own argument
    # (tests/test_tool_argument_echo.py). The answer is a refusal to guess.
    assert "kestrel" not in out
    assert "don't have any work under that name" in out


def test_an_ambiguous_project_asks_which(wired):
    server, store = wired
    _run(store, project="chitauri-api", status=store.RunStatus.RUNNING)
    _run(store, project="chitauri-web", status=store.RunStatus.RUNNING)
    out = _speakable(server.tool_run_status({"run": "chitauri"}))
    assert "chitauri-api" in out and "chitauri-web" in out
    assert out.rstrip().endswith("?")


def test_an_exact_project_name_wins_over_its_own_prefixes(wired):
    server, store = wired
    _run(store, project="chitauri", status=store.RunStatus.RUNNING)
    _run(store, project="chitauri-api", status=store.RunStatus.RUNNING)
    out = _speakable(server.tool_run_status({"run": "chitauri"}))
    assert "Which one?" not in out
    assert "still going" in out


def test_a_failed_run_is_reported_as_failed_with_its_reason(wired):
    server, store = wired
    _run(store, status=store.RunStatus.FAILED, ended_at=time.time() - 120,
         error="ModuleNotFoundError: no such thing")
    out = server.tool_run_status({"run": "chitauri"})
    _speakable(out)
    assert "failed" in out
    assert "ModuleNotFoundError" in out
    assert "untrusted=\"true\"" in out, (
        "stderr from a child process is wrapped like any other foreign output")


def test_a_timed_out_run_says_it_ran_out_of_time(wired):
    server, store = wired
    _run(store, status=store.RunStatus.TIMED_OUT, ended_at=time.time() - 60)
    assert "ran out of time" in _speakable(
        server.tool_run_status({"run": "chitauri"}))


def test_a_queued_run_says_it_has_not_begun(wired):
    server, store = wired
    _run(store)                       # created, still queued
    out = _speakable(server.tool_run_status({"run": "chitauri"}))
    assert "queued" in out


def test_two_runs_in_one_project_are_told_apart_by_their_prompts(wired):
    server, store = wired
    _run(store, prompt="fix the redirect loop on login",
         status=store.RunStatus.RUNNING)
    _run(store, prompt="write the missing tests for billing",
         status=store.RunStatus.RUNNING)
    out = _speakable(server.tool_run_status({"run": "chitauri"}))
    assert "redirect" in out and "billing" in out


def test_a_few_words_of_the_prompt_resolve_a_run(wired):
    server, store = wired
    _run(store, prompt="fix the redirect loop", status=store.RunStatus.RUNNING)
    out = _speakable(server.tool_run_status({"run": "redirect"}))
    assert "chitauri" in out


# --- cancelling ----------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_stops_a_running_run(wired, monkeypatch):
    server, store = wired
    ex = _Executor(store)
    monkeypatch.setattr(server, "run_executor_instance", ex)
    run_id = _run(store, status=store.RunStatus.RUNNING)

    out = _speakable(await server.tool_cancel_run({"run": "chitauri"}))

    assert ex.cancelled == [run_id]
    assert out == "Stopped the work in chitauri, sir."


@pytest.mark.asyncio
async def test_cancel_works_from_a_back_reference(wired, monkeypatch):
    server, store = wired
    ex = _Executor(store)
    monkeypatch.setattr(server, "run_executor_instance", ex)
    monkeypatch.setattr(server, "cached_projects",
                        [{"name": "chitauri", "path": "/tmp/chitauri"}])
    await server.tool_spawn_run({"project": "chitauri", "prompt": "build it"})
    store.update_run(server.last_started_run, status=store.RunStatus.RUNNING)

    out = _speakable(await server.tool_cancel_run({"run": "that"}))

    assert ex.cancelled == [server.last_started_run]
    assert "Stopped" in out


@pytest.mark.asyncio
async def test_cancel_of_an_unknown_id_says_so_and_stops_nothing(wired,
                                                                 monkeypatch):
    server, store = wired
    ex = _Executor(store)
    monkeypatch.setattr(server, "run_executor_instance", ex)
    _run(store, status=store.RunStatus.RUNNING)

    out = _speakable(await server.tool_cancel_run(
        {"run": "b3a1f2c4-0000-4000-8000-000000000000"}))

    assert ex.cancelled == [], "an id nobody recognises stops nothing"
    assert "don't have any work" in out


@pytest.mark.asyncio
async def test_cancel_of_an_already_finished_run_is_honest(wired, monkeypatch):
    server, store = wired
    ex = _Executor(store)
    monkeypatch.setattr(server, "run_executor_instance", ex)
    _run(store, status=store.RunStatus.SUCCEEDED, ended_at=time.time() - 60)

    out = _speakable(await server.tool_cancel_run({"run": "chitauri"}))

    assert ex.cancelled == []
    assert "nothing to stop" in out.lower()
    assert "worked" in out, "it says what actually happened to it"


@pytest.mark.asyncio
async def test_cancel_reports_a_run_that_finished_in_the_race(wired,
                                                              monkeypatch):
    """The executor returns False when the run went terminal first. JARVIS
    must not claim to have stopped something that finished on its own."""
    server, store = wired
    ex = _Executor(store, answer=False)
    monkeypatch.setattr(server, "run_executor_instance", ex)
    _run(store, status=store.RunStatus.RUNNING)

    out = _speakable(await server.tool_cancel_run({"run": "chitauri"}))

    assert "Stopped" not in out
    assert "finished before I could stop it" in out


@pytest.mark.asyncio
async def test_cancel_never_picks_between_two_running_in_one_project(
        wired, monkeypatch):
    server, store = wired
    ex = _Executor(store)
    monkeypatch.setattr(server, "run_executor_instance", ex)
    _run(store, prompt="fix the redirect loop", status=store.RunStatus.RUNNING)
    _run(store, prompt="write the billing tests", status=store.RunStatus.RUNNING)

    out = _speakable(await server.tool_cancel_run({"run": "chitauri"}))

    assert ex.cancelled == [], "ambiguity is a question, not a coin toss"
    assert "which one?" in out.lower()
    # the two are named INSIDE a block: a prompt is prose, and the brain's
    # own (tests/test_tool_argument_echo.py)
    assert "redirect" in out and "billing" in out


@pytest.mark.asyncio
async def test_cancel_with_no_argument_asks(wired, monkeypatch):
    server, store = wired
    ex = _Executor(store)
    monkeypatch.setattr(server, "run_executor_instance", ex)
    out = _speakable(await server.tool_cancel_run({}))
    assert out.rstrip().endswith("?")
    assert ex.cancelled == []


@pytest.mark.asyncio
async def test_cancel_survives_the_executor_throwing(wired, monkeypatch):
    server, store = wired
    monkeypatch.setattr(server, "run_executor_instance",
                        _Executor(store, boom=True))
    _run(store, status=store.RunStatus.RUNNING)
    out = _speakable(await server.tool_cancel_run({"run": "chitauri"}))
    assert "couldn't stop" in out


@pytest.mark.asyncio
async def test_cancelling_removes_it_from_the_pending_announcement(
        wired, monkeypatch):
    """The user was just told it stopped; they must not then be told, at the
    next pause, that it finished."""
    server, store = wired
    monkeypatch.setattr(server, "run_executor_instance", _Executor(store))
    _run(store, status=store.RunStatus.RUNNING)
    server._pending_run_completions[:] = ["chitauri", "hammer"]

    await server.tool_cancel_run({"run": "chitauri"})

    assert server._pending_run_completions == ["hammer"]


# --- the registries ------------------------------------------------------

def test_run_status_is_readable_by_anyone_and_cancel_is_not(wired):
    server, _store = wired
    assert "run_status" in server.TOOL_HANDLERS
    assert "cancel_run" in server.TOOL_HANDLERS
    assert "run_status" not in server.ACTING_TOOLS, (
        "reading is not acting; a watcher turn may still answer 'is it done'")
    assert "cancel_run" in server.ACTING_TOOLS, "it kills a process"


def test_the_three_tool_sets_still_agree(wired):
    import brain
    import jarvis_mcp
    server, _store = wired
    for name in ("run_status", "cancel_run"):
        assert f"mcp__jarvis__{name}" in brain.ALLOWED_TOOLS
    assert {t["name"] for t in jarvis_mcp.TOOL_SPECS} == set(server.TOOL_HANDLERS)
    assert {t for t in brain.ALLOWED_TOOLS if t.startswith("mcp__")} <= {
        f"mcp__jarvis__{n}" for n in server.TOOL_HANDLERS}


@pytest.mark.asyncio
async def test_a_watcher_turn_cannot_cancel_a_run(wired, monkeypatch):
    from fastapi.testclient import TestClient
    import data_paths
    server, store = wired
    ex = _Executor(store)
    _run(store, status=store.RunStatus.RUNNING)

    class _Brain:
        current_origin = "watcher"
        ready = False

        async def stop(self):
            pass

    token = data_paths.ensure_tool_token()
    with TestClient(server.app) as client:
        monkeypatch.setattr(server, "run_executor_instance", ex)
        monkeypatch.setattr(server, "brain_instance", _Brain())
        r = client.post("/internal/tool",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"tool": "cancel_run",
                              "arguments": {"run": "chitauri"}})

    assert r.json()["ok"] is False
    assert "not_allowed_from_event" in r.json()["text"]
    assert ex.cancelled == []
