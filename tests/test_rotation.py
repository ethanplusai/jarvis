"""Context budget and rotation.

The brain's window grows with every turn, which makes it slower and burns the
user's subscription. Crossing the budget *schedules* a rotation; the rotation
itself is performed at a conversational pause, so it can never cut the user off
mid-sentence.

Reuses the fake-brain harness from tests/test_brain.py — there is exactly one
stand-in `claude` in this suite.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.test_brain import _alive, _config, _wait_until  # noqa: E402

import brain  # noqa: E402


@pytest.mark.asyncio
async def test_crossing_the_budget_schedules_a_rotation_but_does_not_perform_it(tmp_path):
    """Rotation must never interrupt a conversation. Crossing the budget only
    raises the flag; the pause performs it."""
    b = brain.Brain(_config(tmp_path, context_budget=10))
    try:
        states = []
        b.on_state(lambda s, info: states.append(s))
        await b.start()

        await b.turn("hello")                       # the fake reports context tokens

        assert b.rotation_pending is True
        assert "rotation_needed" in states
        assert b.generation == 1, "still the same process"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_rotation_swaps_in_a_new_generation(tmp_path):
    b = brain.Brain(_config(tmp_path, context_budget=10))
    try:
        await b.start()
        await b.turn("hello")
        first_gen = b.generation

        assert await b.rotate(handover="we were fixing chitauri") is True

        assert b.generation == first_gen + 1
        assert b.rotation_pending is False
        assert b.ready is True
        assert (await b.turn("still there?")).stop_reason == "result"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_the_handover_reaches_the_new_generation(tmp_path):
    b = brain.Brain(_config(tmp_path, context_budget=10))
    try:
        await b.start()
        await b.turn("hello")

        await b.rotate(handover="we were fixing chitauri")

        assert "chitauri" in b.launch_prompt()
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_the_handover_is_passed_to_the_new_process(tmp_path, monkeypatch):
    """launch_prompt() is only useful if the spawn actually carries it: the
    handover reaches the new generation through --append-system-prompt."""
    seen = []
    real = asyncio.create_subprocess_exec

    async def capture(*argv, **kw):
        seen.append(list(argv))
        return await real(*argv, **kw)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    b = brain.Brain(_config(tmp_path, context_budget=10))
    try:
        await b.start()
        await b.turn("hello")

        assert await b.rotate(handover="we were fixing chitauri") is True

        prompt = seen[-1][seen[-1].index("--append-system-prompt") + 1]
        assert "chitauri" in prompt and "brain generation 2" in prompt
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_a_rotation_cannot_race_a_turn(tmp_path):
    """Turns are serialised; rotation takes the same lock, so a turn in flight
    completes against the process that started it."""
    b = brain.Brain(_config(tmp_path, context_budget=10))
    try:
        await b.start()

        turn = asyncio.create_task(b.turn("SLOW:1 hello"))
        await asyncio.sleep(0.05)
        rot = asyncio.create_task(b.rotate(handover="x"))
        result = await turn
        assert await rot is True

        assert result.stop_reason == "result", "the in-flight turn was not killed"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_a_failed_standby_leaves_the_current_brain_serving(tmp_path, monkeypatch):
    """If the replacement will not start, keep the one that works rather than
    leaving JARVIS mute."""
    marker = tmp_path / "die-once"
    monkeypatch.setenv("FAKE_BRAIN_DIE_ONCE", str(marker))
    b = brain.Brain(_config(tmp_path, context_budget=10))
    try:
        await b.start()
        await b.turn("hello")
        gen = b.generation
        pid = b._proc.pid
        marker.write_text("x", encoding="utf-8")                    # the replacement exits 1 at startup

        assert await b.rotate(handover="x") is False

        assert not marker.exists(), "the replacement really was spawned"
        assert b.ready is True and b.generation == gen
        assert b._proc.pid == pid, "still the process that works"
        assert (await b.turn("still there?")).stop_reason == "result"
        assert b.rotation_pending is True, "the rotation is still owed"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_a_failed_rotation_never_announces_a_dead_brain(tmp_path, monkeypatch):
    """`failed` makes JARVIS say its language systems are down and refuse every
    turn. A rotation that could not spawn has a working brain to fall back on,
    so it must never raise that verdict — even when the binary has gone."""
    b = brain.Brain(_config(tmp_path, context_budget=10))
    try:
        states = []
        b.on_state(lambda s, info: states.append(s))
        await b.start()
        await b.turn("hello")
        b._claude = "/definitely/not/here/claude"          # the CLI vanished mid-session

        assert await b.rotate(handover="x") is False

        assert b.failed is False and "failed" not in states
        assert b.ready is True
        assert (await b.turn("still there?")).stop_reason == "result"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_a_failed_rotation_does_not_burn_the_restart_budget(tmp_path, monkeypatch):
    """The stillborn replacement's exit must not look like a crash: three
    failed rotations would otherwise exhaust max_restarts and mute a brain
    that is serving perfectly well."""
    marker = tmp_path / "die-once"
    monkeypatch.setenv("FAKE_BRAIN_DIE_ONCE", str(marker))
    b = brain.Brain(_config(tmp_path, context_budget=10, max_restarts=3))
    try:
        await b.start()
        await b.turn("hello")
        for _ in range(4):
            marker.write_text("x", encoding="utf-8")
            assert await b.rotate(handover="x") is False
        await asyncio.sleep(1.0)                  # past any restart backoff

        assert b._restart_times == [] and not b.failed
        assert b.ready is True
        assert (await b.turn("still there?")).stop_reason == "result"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_a_rotation_that_loses_both_processes_is_handed_back_to_the_restarter(
        tmp_path, monkeypatch):
    """The predecessor dying inside the rotation window schedules nothing — it
    is detached on purpose. If the replacement then fails too there is nothing
    serving, and the brain must recover instead of staying mute forever."""
    marker = tmp_path / "die-once"
    monkeypatch.setenv("FAKE_BRAIN_DIE_ONCE", str(marker))
    b = brain.Brain(_config(tmp_path, context_budget=10))
    try:
        await b.start()
        await b.turn("hello")
        old = b._proc
        real = asyncio.create_subprocess_exec

        async def kill_the_predecessor(*argv, **kw):
            try:
                old.kill()                     # it dies as the replacement starts
            except ProcessLookupError:
                pass
            return await real(*argv, **kw)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", kill_the_predecessor)
        marker.write_text("x", encoding="utf-8")                 # ...and the replacement will not start

        assert await b.rotate(handover="x") is False

        monkeypatch.undo()
        assert await _wait_until(lambda: b.ready, 8.0), "left mute with no restart"
        assert (await b.turn("still there?")).stop_reason == "result"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_the_superseded_process_is_not_orphaned(tmp_path):
    b = brain.Brain(_config(tmp_path, context_budget=10))
    try:
        await b.start()
        await b.turn("hello")
        old_pid = b._proc.pid

        assert await b.rotate(handover="x") is True

        new_pid = b._proc.pid
        assert new_pid != old_pid
        assert await _wait_until(lambda: not _alive(old_pid), 3.0), "superseded child still alive"
    finally:
        await b.stop()
    assert await _wait_until(lambda: not _alive(new_pid), 3.0)


@pytest.mark.asyncio
async def test_rotating_a_brain_that_is_not_serving_is_refused(tmp_path):
    """Nothing to rotate: rotating a brain that never started, or one that has
    given up, would spawn a process outside the restart machinery."""
    b = brain.Brain(_config(tmp_path, context_budget=10))
    try:
        assert await b.rotate(handover="x") is False        # never started
        assert b.generation == 0

        await b.start()
        b._failed = True
        assert await b.rotate(handover="x") is False
        assert b.generation == 1
        b._failed = False
    finally:
        await b.stop()
    assert await b.rotate(handover="x") is False        # stopped
    assert b.generation == 1


@pytest.mark.asyncio
async def test_rotation_is_forced_after_too_many_waiting_turns(tmp_path):
    """A conversation that never pauses must still rotate eventually."""
    b = brain.Brain(_config(tmp_path, context_budget=10,
                            max_turns_before_forced_rotation=3))
    try:
        await b.start()
        for _ in range(4):
            await b.turn("hello")

        assert b.rotation_overdue is True
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_rotation_is_not_overdue_before_the_budget_is_crossed(tmp_path):
    """The forced-rotation counter only starts once a rotation is owed."""
    b = brain.Brain(_config(tmp_path, context_budget=10_000_000,
                            max_turns_before_forced_rotation=1))
    try:
        await b.start()
        for _ in range(3):
            await b.turn("hello")

        assert b.rotation_overdue is False and b.turns_since_rotation == 0
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_a_brain_under_budget_never_schedules_a_rotation(tmp_path):
    b = brain.Brain(_config(tmp_path, context_budget=10_000_000))
    try:
        await b.start()
        await b.turn("hello")

        assert b.rotation_pending is False and b.rotation_overdue is False
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_the_warmup_alone_never_schedules_a_rotation(tmp_path):
    """A fresh process is measured by its own warm-up. Counting that against
    the budget would mark every new generation as needing another one."""
    b = brain.Brain(_config(tmp_path, context_budget=10))
    try:
        await b.start()

        assert b.rotation_pending is False, "no conversation has happened yet"
        await b.turn("hello")
        assert b.rotation_pending is True
        assert await b.rotate() is True
        assert b.rotation_pending is False, "the new generation starts owing nothing"
    finally:
        await b.stop()


def test_the_budget_defaults_and_reads_the_environment(tmp_path, monkeypatch):
    assert brain.BrainConfig(home=tmp_path).context_budget == 120000
    assert brain.BrainConfig(home=tmp_path).max_turns_before_forced_rotation == 10
    assert brain.BrainConfig.from_env(tmp_path).context_budget == 120000
    monkeypatch.setenv("JARVIS_BRAIN_CONTEXT_BUDGET", "25000")
    assert brain.BrainConfig.from_env(tmp_path).context_budget == 25000


def test_a_cache_rebuild_is_not_counted_as_the_conversation_growing():
    """The same prompt is reported as WRITTEN when it misses the cache and
    as READ when it hits. Neither is growth: the window is all three
    columns, and it is the same size either way.

    This test used to pin the opposite rule — the window was input plus
    cache_read, and a cache-miss turn measured 500 tokens. That is exactly
    what generation 1 of 2026-09-25 did on a cold boot: its warm-up missed,
    its floor was logged as `ctx=2`, and every turn after it read the
    99,000-token floor back as conversation. The over-count the old rule was
    written for (a 60k budget rotating at ~30k of talk) was the CLI's sum
    over a turn's API calls; tests/test_rotation_loop.py covers that."""
    floor = brain._Turn("user", None)
    # The warm-up on a cold cache: the whole floor written, none of it read.
    floor.usage = {"input_tokens": 500, "cache_read_input_tokens": 0,
                   "cache_creation_input_tokens": 29_000, "output_tokens": 40}
    assert floor.context_tokens() == 29_500, (
        "a floor that was written to the cache is still the floor")
    # The next turn reads that cache back, and has added nothing.
    hit = brain._Turn("user", None)
    hit.usage = {"input_tokens": 500, "cache_read_input_tokens": 29_000,
                 "cache_creation_input_tokens": 0, "output_tokens": 40}
    assert hit.context_tokens() - floor.context_tokens() == 0, (
        "reading back what was written is not the conversation growing")
    # A cache that lapses mid-conversation is rebuilt: the whole prompt is
    # written again. The window is what it was plus what was said.
    rebuilt = brain._Turn("user", None)
    rebuilt.usage = {"input_tokens": 10, "cache_read_input_tokens": 0,
                     "cache_creation_input_tokens": 29_490 + 1_200,
                     "output_tokens": 40}
    assert rebuilt.context_tokens() - floor.context_tokens() == 1_200


@pytest.mark.asyncio
async def test_the_budget_is_spent_on_talk_not_on_cache_churn(tmp_path, monkeypatch):
    """End to end through the real Brain with the fake, which reports 1,000
    tokens of cache creation on every turn, the warm-up included. They are
    in the floor and in every turn alike, so they cancel: with the budget
    set just above one turn of talk, cache churn must not tip it over.

    The warm-up misses the cache, as every cold boot does: its floor is
    reported as WRITTEN and the next turn reads it back. A measure that left
    cache creation out took that floor as 10 tokens and the next turn's
    19,000 as all conversation — this test rotated under it."""
    monkeypatch.setenv("FAKE_BRAIN_COLD_WARMUP", "1")
    b = brain.Brain(_config(tmp_path, context_budget=9_500))
    try:
        await b.start()
        # The fake's window grows 9,000/turn in cache_read; the warm-up is turn 1.
        # One conversational turn is 9,000 of real growth -- under 9,500. The
        # 1,000 of cache_creation is in the floor too, so it is not growth.
        await b.turn("hello")
        assert b.rotation_pending is False, (
            f"rotated on cache churn: conversation={b.conversation_tokens} "
            f"baseline={b.baseline_tokens}")
        await b.turn("and again")                   # 18,000 of real growth: over
        assert b.rotation_pending is True
    finally:
        await b.stop()
