"""A brain killed mid-conversation comes back in the SAME conversation.

The live failure this pins: a voice turn that ran past the turn limit (Opus
poking at the Clock app for a two-minute timer) got the process killed, and
the replacement started blank, so JARVIS forgot everything said before it.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

FAKE = Path(__file__).parent / "fixtures" / "fake_brain.py"


def _config(tmp_path, **kw):
    import brain
    return brain.BrainConfig(home=tmp_path / "jarvis",
                             claude_path=f"{sys.executable} {FAKE}",
                             turn_timeout=kw.pop("turn_timeout", 5.0),
                             warmup_timeout=kw.pop("warmup_timeout", 10.0),
                             **kw)

SESSION = "fake-session-0001"


def _record_commands(b):
    calls = []
    real = b.command

    def spy(resume=None):
        calls.append(resume)
        return real(resume)

    b.command = spy
    return calls


async def _wait_ready_past(b, gen, n=1):
    for _ in range(100):
        if b.ready and b.generation >= gen + n:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"brain not back: gen={b.generation} ready={b.ready}")


def test_command_resumes_only_when_asked(tmp_path):
    import brain
    b = brain.Brain(_config(tmp_path))
    assert "--resume" not in b.command()
    cmd = b.command(SESSION)
    assert cmd[cmd.index("--resume") + 1] == SESSION


@pytest.mark.asyncio
async def test_stuck_turn_restart_resumes_the_conversation(tmp_path, monkeypatch):
    import brain
    warmups = []
    real_turn = brain.Brain._turn

    async def spy_turn(self, text, *a, **kw):
        if kw.get("warmup"):
            warmups.append(text)
        return await real_turn(self, text, *a, **kw)

    monkeypatch.setattr(brain.Brain, "_turn", spy_turn)
    b = brain.Brain(_config(tmp_path, turn_timeout=0.5))
    calls = _record_commands(b)
    try:
        await b.start()
        gen = b.generation
        assert (await b.turn("SLOW:3 set a timer")).stop_reason == "timeout"
        await _wait_ready_past(b, gen)
        assert calls == [None, SESSION]
        assert warmups == [brain.WARMUP_TEXT, brain.RESUME_WARMUP_TEXT]
        assert (await b.turn("hello again")).stop_reason == "result"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_a_resume_that_fails_falls_back_to_a_fresh_brain(tmp_path, monkeypatch):
    import brain
    # The stand-in exits on any turn containing DIE: the resumed warm-up fails.
    monkeypatch.setattr(brain, "RESUME_WARMUP_TEXT", "DIE")
    b = brain.Brain(_config(tmp_path, turn_timeout=0.5))
    calls = _record_commands(b)
    try:
        await b.start()
        gen = b.generation
        assert (await b.turn("SLOW:3 stuck")).stop_reason == "timeout"
        await _wait_ready_past(b, gen, n=2)
        assert calls == [None, SESSION, None]
        assert b.ready and not b.failed
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_a_session_that_dies_again_before_answering_is_not_resumed_twice(tmp_path):
    import brain
    b = brain.Brain(_config(tmp_path, turn_timeout=0.5))
    calls = _record_commands(b)
    try:
        await b.start()
        gen = b.generation
        assert (await b.turn("SLOW:3 one")).stop_reason == "timeout"
        await _wait_ready_past(b, gen)
        gen = b.generation
        assert (await b.turn("SLOW:3 two")).stop_reason == "timeout"
        await _wait_ready_past(b, gen)
        assert calls == [None, SESSION, None]
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_an_answer_after_resuming_rearms_the_next_resume(tmp_path):
    import brain
    b = brain.Brain(_config(tmp_path, turn_timeout=0.5))
    calls = _record_commands(b)
    try:
        await b.start()
        gen = b.generation
        assert (await b.turn("SLOW:3 one")).stop_reason == "timeout"
        await _wait_ready_past(b, gen)
        assert (await b.turn("fine")).stop_reason == "result"
        gen = b.generation
        assert (await b.turn("SLOW:3 two")).stop_reason == "timeout"
        await _wait_ready_past(b, gen)
        assert calls == [None, SESSION, SESSION]
    finally:
        await b.stop()
