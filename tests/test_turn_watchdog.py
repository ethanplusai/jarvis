"""The turn watchdog measures SILENCE, and is held while a tool is out.

Measured live, 2026-09-22. JARVIS was asked for a LinkedIn dry run. The
turn ran 90.02s, the watchdog called it stuck, killed the brain and
restarted it, and the user got "I lost my train of thought, sir."

Nothing was stuck. The brain was waiting on `mcp__linkedin__create_post`,
which that server allows 180s for — twice what a whole turn was given. The
brain's own tools cannot do this: they are an allowlist (brain.py:45-99)
and every `mcp__jarvis__*` call is hard-bounded at 20s inside the MCP child
and returns a text error rather than hanging. The unbounded ones are the
user's own declared servers, which never touch JARVIS's code at all.

So time was the wrong measure. A brain emitting deltas and dispatching
tools is alive however long it takes; a brain emitting nothing for the
budget is stuck whether it is 20s in or 200. The rule is now:

  - silent with nothing outstanding      -> stuck, kill and restart
  - silent with a tool outstanding       -> held, it is the tool's clock
  - past the absolute ceiling, regardless -> stuck

The hold needs the ceiling. Without one this recreates the hang the
original `wait_for` existed to bound: `_turn_lock` is held for the whole
turn, `rotate()` wants that lock, and the escape hatch counts ten further
turns that can never run.

And the reason this file exists rather than a few more cases in
test_brain.py: until now the fake brain could not emit a tool_use and go
quiet, so a watchdog that held forever and never fired would have passed
the entire suite. HANGTOOL / SLOWTOOL / TWOTOOLS in tests/fixtures/
fake_brain.py are that missing case.
"""

import asyncio
import time

import pytest

from tests.test_brain import _config


@pytest.mark.asyncio
async def test_a_healthy_long_tool_is_not_killed(tmp_path):
    """The reported failure. Silent for well over the budget, then it lands."""
    import brain
    b = brain.Brain(_config(tmp_path, turn_timeout=0.5, turn_ceiling=30.0))
    try:
        await b.start()
        gen = b.generation
        r = await b.turn("SLOWTOOL:2.0 post it")
        assert r.stop_reason == "result", "a tool in flight is not a stuck brain"
        assert b.generation == gen, "the brain must not have been restarted"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_silence_with_nothing_outstanding_still_dies_on_the_silence_budget(tmp_path):
    """And dies ON the budget, not on the ceiling — otherwise the silence
    rule is inert and nothing in the suite would notice."""
    import brain
    b = brain.Brain(_config(tmp_path, turn_timeout=0.5, turn_ceiling=30.0))
    try:
        await b.start()
        started = time.monotonic()
        r = await b.turn("SLOW:5 never")
        elapsed = time.monotonic() - started
        assert r.stop_reason == "timeout"
        assert elapsed < 3.0, (
            f"took {elapsed:.1f}s: it waited for the ceiling, not the silence budget")
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_a_tool_that_never_returns_still_dies_at_the_ceiling(tmp_path):
    """The failure mode the hold creates. Held is not forgiven."""
    import brain
    b = brain.Brain(_config(tmp_path, turn_timeout=0.5, turn_ceiling=2.0))
    try:
        await b.start()
        gen = b.generation
        r = await b.turn("HANGTOOL wedge")
        assert r.stop_reason == "timeout"
        for _ in range(60):
            if b.ready and b.generation > gen:
                break
            await asyncio.sleep(0.1)
        assert b.generation == gen + 1 and b.ready, "a real wedge must still recover"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_two_tools_in_one_message_hold_until_both_return(tmp_path):
    """`on_tool` fires once per BLOCK, and one assistant message can carry
    two. A hold expressed as a boolean releases on the first result while
    the second call is still outstanding."""
    import brain
    b = brain.Brain(_config(tmp_path, turn_timeout=0.5, turn_ceiling=3.0))
    try:
        await b.start()
        started = time.monotonic()
        r = await b.turn("TWOTOOLS both")
        elapsed = time.monotonic() - started
        assert r.stop_reason == "timeout", "it never completes; the ceiling ends it"
        # Near the CEILING, not merely past the fixture's first sleep. With a
        # boolean hold the fixture's tool_result at t=1.0s releases it and
        # the 0.5s silence budget ends the turn at ~1.55s — which cleared a
        # `> 1.5` threshold, so the test named for that exact regression
        # reported green while the regression was present. Only running to
        # the ceiling distinguishes the two.
        assert elapsed > 2.5, (
            f"gave up after {elapsed:.1f}s, short of the 3.0s ceiling: the "
            f"hold released while the second tool was still outstanding")
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_the_hold_does_not_leak_into_the_next_turn(tmp_path):
    """The count belongs to the TURN. A turn that ends with a tool still out
    must not leave the next one permanently held."""
    import brain
    b = brain.Brain(_config(tmp_path, turn_timeout=0.5, turn_ceiling=2.0))
    try:
        await b.start()
        assert (await b.turn("HANGTOOL wedge")).stop_reason == "timeout"
        for _ in range(60):
            if b.ready:
                break
            await asyncio.sleep(0.1)
        started = time.monotonic()
        r = await b.turn("SLOW:5 never")
        elapsed = time.monotonic() - started
        assert r.stop_reason == "timeout"
        assert elapsed < 3.0, (
            f"took {elapsed:.1f}s: the previous turn's outstanding tool is "
            f"still holding this one")
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_the_stop_reason_is_still_timeout(tmp_path):
    """server.py branches on ("timeout", "died", "not_running") to speak the
    restart lines. A new value falls through to the empty-text branch and
    JARVIS says NOTHING AT ALL, with a green suite — no test drives a
    non-result stop_reason through _handle_utterance."""
    import brain
    b = brain.Brain(_config(tmp_path, turn_timeout=0.5, turn_ceiling=2.0))
    try:
        await b.start()
        assert (await b.turn("SLOW:5 never")).stop_reason == "timeout"
    finally:
        await b.stop()


def test_the_ceiling_is_a_shipped_default_with_a_documented_knob():
    """`JARVIS_BRAIN_TURN_TIMEOUT` shipped undocumented and untested — one
    occurrence repo-wide. The knob that replaces it does not get to."""
    import io
    import brain
    from pathlib import Path
    cfg = brain.BrainConfig(home=Path("."))
    assert cfg.turn_timeout == 90.0, "the silence budget keeps the shipped value"
    assert cfg.turn_ceiling == 300.0
    assert cfg.turn_ceiling > cfg.turn_timeout, "a ceiling below the budget is unreachable"
    for name in ("JARVIS_BRAIN_TURN_TIMEOUT", "JARVIS_BRAIN_TURN_CEILING"):
        for path in ("CLAUDE.md", ".env.example"):
            assert name in io.open(path, encoding="utf-8").read(), f"{name} missing from {path}"


# --- the result and the clock landing together ---------------------------
#
# `_Turn.finish` is first-write-wins (brain.py), which is what makes the
# timeout path's `finish("timeout")` beat `_on_exit`'s later
# `finish("died")`. It cuts the other way too: if a `result` event lands in
# the same tick the budget runs out, `done` is already set, `finish("timeout")`
# is a no-op, and the turn reports `stop_reason == "result"` — correctly,
# because it DID finish — while the watchdog goes on to kill and restart a
# brain that had just answered.
#
# Nobody hears it. The user gets their reply. But it spends one of three
# restarts inside a 300s window, and exhausting those sets `_failed`, which
# is permanent for that Brain. A brain that answers slightly late every time
# retires itself in under two minutes.

@pytest.mark.asyncio
async def test_a_result_landing_as_the_clock_runs_out_is_not_a_timeout(tmp_path):
    import brain

    class _Stdin:
        def write(self, b): pass
        async def drain(self): pass

    class _Proc:
        stdin = _Stdin()

    t = brain._Turn("user", None, proc=None)
    t.finish("result")                      # the answer landed
    assert t.wait_slice(0.0, 0.0) == 0.0, "the budget is exhausted at the same moment"

    await brain.Brain._send_and_wait(_Proc(), "{}", t, 0.0, 0.0)
    assert t.stop_reason == "result"


@pytest.mark.asyncio
async def test_a_turn_that_really_is_silent_still_times_out(tmp_path):
    """The guard must not swallow a real expiry."""
    import brain

    class _Stdin:
        def write(self, b): pass
        async def drain(self): pass

    class _Proc:
        stdin = _Stdin()

    t = brain._Turn("user", None, proc=None)
    with pytest.raises(asyncio.TimeoutError):
        await brain.Brain._send_and_wait(_Proc(), "{}", t, 0.0, 0.0)


@pytest.mark.asyncio
async def test_a_late_answer_does_not_spend_the_restart_budget(tmp_path):
    """End to end: the fake child answers slightly after the silence budget
    while a tool is outstanding, and the brain must survive it."""
    import brain
    b = brain.Brain(_config(tmp_path, turn_timeout=0.4, turn_ceiling=30.0))
    try:
        await b.start()
        gen, restarts = b.generation, len(b._restart_times)
        r = await b.turn("SLOWTOOL:1.2 answer late")
        assert r.stop_reason == "result"
        assert b.generation == gen, "the brain was restarted for answering late"
        assert len(b._restart_times) == restarts, "a restart was spent on a healthy turn"
    finally:
        await b.stop()


def test_the_log_says_which_budget_actually_ran_out(tmp_path):
    """"brain: turn stuck for 90.0s" was printed for a turn that ran 300s
    and was never silent for more than a moment: the line always named the
    SILENCE budget, even when the ceiling was what fired. Whoever reads that
    log is being told to look at the wrong number."""
    import inspect
    import brain
    source = inspect.getsource(brain.Brain._turn_locked)
    assert "ceiling" in source, \
        "the stuck line cannot distinguish the two budgets it is bounded by"
