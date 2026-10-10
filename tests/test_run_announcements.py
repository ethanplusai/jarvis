"""JARVIS saying when work he started has ended.

Before this, the only subscriber to the run executor was the dashboard's
WebSocket: JARVIS would start a run and then never mention it again.

Two things are load-bearing.

**Origin.** Only runs JARVIS himself started (origin "voice") are narrated.
The user runs plenty of other things — from the dashboard, from work mode,
from a terminal — and those are not his to talk about.

**Which thread the executor calls back on.** The session watcher shipped the
opposite arrangement once: its callback fired on a poller thread, where
`asyncio.create_task` raises RuntimeError, the error was swallowed, and
announcements silently never happened while every test passed. So this file
asserts the property rather than assuming it — `test_the_executor_publishes_
on_the_event_loop_thread` drives a REAL RunExecutor and checks that
`asyncio.get_running_loop()` succeeds inside the subscriber.
"""

import asyncio
import importlib
import stat
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

FIXTURE = Path(__file__).parent / "fixtures" / "stream_success.jsonl"


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server as server_module
    importlib.reload(server_module)
    run_store.init_db()
    server_module._pending_completions.clear()
    server_module._pending_run_completions.clear()
    return server_module


class FakeSpeech:
    def __init__(self, boom=False):
        self.calls = []
        self.boom = boom

    async def say(self, text, priority=None, **k):
        self.calls.append((text, priority))
        if self.boom:
            raise RuntimeError("the mouth is broken")


def _finished(project="chitauri", status="succeeded", origin="voice", **over):
    run = {"id": "r-1", "project_name": project, "status": status,
           "origin": origin, "error": "", "prompt": "build it"}
    run.update(over)
    return {"type": "run_finished", "run": run}


async def _drain(server):
    """Let the fire-and-forget announcement tasks run to completion."""
    for _ in range(10):
        pending = [t for t in server._bg_tasks if not t.done()]
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)


# --- the origin gate on narration ----------------------------------------

@pytest.mark.asyncio
async def test_a_run_jarvis_started_is_announced(wired, monkeypatch):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)

    server._on_run_event(_finished())
    await _drain(server)

    assert speech.calls, "he started it; he says when it is done"
    text, priority = speech.calls[0]
    assert "chitauri" in text
    assert priority == server.Priority.LOW, "a success can wait for the pause"


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["api", "self", "dashboard", "", None])
async def test_a_run_jarvis_did_not_start_is_not_announced(wired, monkeypatch,
                                                           origin):
    """The user runs things of their own. Those are not his to narrate."""
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)

    server._on_run_event(_finished(origin=origin))
    await _drain(server)

    assert speech.calls == []
    assert server._pending_run_completions == []


@pytest.mark.asyncio
async def test_only_finished_messages_are_narrated(wired, monkeypatch):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)

    server._on_run_event({"type": "run_started", "run": _finished()["run"]})
    server._on_run_event({"type": "run_event", "run_id": "r-1"})
    server._on_run_event({"type": "run_updated", "run": _finished()["run"]})
    await _drain(server)

    assert speech.calls == []


# --- what he actually says ------------------------------------------------

@pytest.mark.asyncio
async def test_a_success_says_the_project_and_that_it_worked(wired, monkeypatch):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)

    server._on_run_event(_finished(project="chitauri"))
    await _drain(server)

    assert speech.calls[0][0] == "The work in chitauri is done, sir."


@pytest.mark.asyncio
async def test_a_failure_interrupts_and_says_it_failed(wired, monkeypatch):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)

    server._on_run_event(_finished(status="failed"))
    await _drain(server)

    text, priority = speech.calls[0]
    assert "chitauri" in text and "failed" in text
    assert priority == server.Priority.URGENT, (
        "a failure the user could be fixing must not wait for a pause")


@pytest.mark.asyncio
async def test_a_timeout_is_announced_as_running_out_of_time(wired, monkeypatch):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)

    server._on_run_event(_finished(status="timed_out"))
    await _drain(server)

    assert "ran out of time" in speech.calls[0][0]
    assert speech.calls[0][1] == server.Priority.URGENT


@pytest.mark.asyncio
async def test_a_cancelled_run_is_not_announced(wired, monkeypatch):
    """The user asked for it and was told at the time. Saying so again at the
    next pause is noise."""
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)

    server._on_run_event(_finished(status="cancelled"))
    await _drain(server)

    assert speech.calls == []


# --- batching -------------------------------------------------------------

@pytest.mark.asyncio
async def test_several_completions_become_one_sentence(wired, monkeypatch):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)

    server._pending_run_completions.extend(["chitauri", "hammer", "kestrel"])
    await server._announce_batch()

    assert len(speech.calls) == 1, "three finishes, one sentence"
    text = speech.calls[0][0]
    assert "chitauri" in text and "hammer" in text and "kestrel" in text
    assert server._pending_run_completions == []


@pytest.mark.asyncio
async def test_a_run_and_a_conversation_share_one_utterance(wired, monkeypatch):
    """Two parallel batchers would mean two LOW announcements back to back at
    every pause."""
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)

    server._pending_completions.append("hammer")
    server._pending_run_completions.append("chitauri")
    await server._announce_batch()

    assert len(speech.calls) == 1
    text = speech.calls[0][0]
    assert "hammer has finished" in text
    assert "The work in chitauri is done" in text


@pytest.mark.asyncio
async def test_many_completions_cap_at_three_names(wired, monkeypatch):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)

    server._pending_run_completions.extend(
        ["a", "b", "c", "d", "e"])
    await server._announce_batch()

    text = speech.calls[0][0]
    assert "two others" in text


@pytest.mark.asyncio
async def test_the_same_project_twice_is_mentioned_once(wired, monkeypatch):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)

    server._on_run_event(_finished(project="chitauri"))
    server._on_run_event(_finished(project="chitauri"))
    await _drain(server)

    assert speech.calls[0][0].count("chitauri") == 1


@pytest.mark.asyncio
async def test_a_lost_announcement_is_not_thrown_away(wired, monkeypatch):
    server = wired
    speech = FakeSpeech(boom=True)
    monkeypatch.setattr(server, "speech", speech)

    server._pending_run_completions.append("chitauri")
    await server._announce_batch()

    assert server._pending_run_completions == ["chitauri"], (
        "it will be said at the next pause instead")


# --- nothing here may reach the executor ---------------------------------

@pytest.mark.asyncio
async def test_a_broken_announcement_never_escapes_the_callback(wired,
                                                                monkeypatch):
    server = wired

    def explode(coro):
        coro.close()                      # never leave it un-awaited
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "_spawn", explode)
    server._on_run_event(_finished())          # must not raise


@pytest.mark.asyncio
async def test_no_speech_configured_is_silent_not_fatal(wired, monkeypatch):
    server = wired
    monkeypatch.setattr(server, "speech", None)
    server._on_run_event(_finished())
    await _drain(server)
    server._on_run_event(_finished(status="failed"))
    await _drain(server)


def test_the_voice_path_is_actually_subscribed(wired):
    """The wiring itself. Everything above tests `_on_run_event`; if nothing
    ever calls it, all of it is theatre."""
    server = wired
    assert server._on_run_event in server.run_executor_instance._subscribers


# --- the thread the executor calls back on -------------------------------

def _fake_claude(tmp_path: Path) -> str:
    script = tmp_path / "fake_claude_for_announcements.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stdout.write(open({str(FIXTURE)!r}).read())\n"
        "sys.stdout.flush()\n"
        "sys.exit(0)\n"
    , encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return f"{sys.executable} {script}"


@pytest.fixture
def real_executor(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    run_store.init_db()
    import run_executor
    importlib.reload(run_executor)
    return run_store, run_executor


@pytest.mark.asyncio
async def test_the_executor_publishes_on_the_event_loop_thread(real_executor,
                                                               tmp_path):
    """The evidence behind `_on_run_event` using `_spawn` with no thread hop.

    Every publish comes out of `_finish` or `_publish_run_updated`, both
    reached only from inside the `_drive` task or `cancel()` — coroutines. If
    that ever stopped being true, `asyncio.create_task` inside the subscriber
    would raise RuntimeError on a worker thread and every announcement would
    silently stop, which is exactly the bug the session watcher shipped.
    """
    store, mod = real_executor
    here = threading.get_ident()
    loop = asyncio.get_running_loop()
    seen = []

    def cb(message):
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        seen.append((message["type"], threading.get_ident(), running))

    ex = mod.RunExecutor(store, claude_path=_fake_claude(tmp_path))
    ex.subscribe(cb)
    run_id = await ex.spawn("do a thing", "proj", str(tmp_path), "voice")
    await ex.wait_for(run_id)

    assert seen, "the executor published something"
    assert any(kind == "run_finished" for kind, _, _ in seen)
    for kind, ident, running in seen:
        assert ident == here, f"{kind} was published off the event loop thread"
        assert running is loop, f"{kind} had no running loop to schedule on"


@pytest.mark.asyncio
async def test_a_real_run_finishing_reaches_the_voice(real_executor, tmp_path,
                                                      monkeypatch):
    """End to end, with a real executor: `_on_run_event` is called by the real
    publish path, `_spawn` succeeds there, and the sentence is spoken."""
    store, mod = real_executor
    import server as server_module
    importlib.reload(server_module)
    speech = FakeSpeech()
    monkeypatch.setattr(server_module, "speech", speech)
    server_module._pending_run_completions.clear()

    ex = mod.RunExecutor(store, claude_path=_fake_claude(tmp_path))
    ex.subscribe(server_module._on_run_event)
    run_id = await ex.spawn("do a thing", "chitauri", str(tmp_path), "voice")
    await ex.wait_for(run_id)
    await _drain(server_module)

    assert speech.calls, "a real completion never reached the voice"
    assert "chitauri" in speech.calls[0][0]
