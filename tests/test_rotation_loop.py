"""The rotation loop of 2026-09-25/26: every turn landed on a new brain.

What the log said, five times over:

    rotation scheduled: conversation=322435 budget=120000 floor=77042
    brain ready: gen=4 ... ctx=77042
    rotation did not happen; retrying at the next pause

(that last line false every time) and the user watched JARVIS forget, one turn later, an approval card it had
staged itself. The generations' own transcripts show what was really in the
window: 1,000 to 26,000 tokens of conversation against a 120,000 budget.
Every one of those rotations was spurious, and two measurement errors made
them:

1. `context_tokens` came from the CLI's `result` event, whose `usage` is the
   SUM over every API call the turn made. A turn that used three tools made
   four calls and reported four copies of a 99,000-token prompt: any turn
   that touched a tool read as "over budget", including the first turn of a
   generation that had only just been born.
2. The window was taken as input + cache_read, leaving out
   cache_creation. The three are disjoint; the prompt is all three. A warm-up
   that missed the cache (every cold boot) measured its floor as 2 tokens,
   and a warm one measured 77,042 of a real 99,073.

These tests use the live numbers where they have them, and the stand-in CLI
(which now speaks the measured shape: per-call usage on every assistant
event, the sum on the result) where they need a process.
"""
import asyncio
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.test_brain import _config  # noqa: E402

import brain  # noqa: E402

# The live shape: 99,000 tokens of system prompt, CLAUDE.md and 119 tool
# schemas before a word is said. With it the fake's warm-up measures 100,010.
LIVE_FLOOR = 90_000
LIVE_BUDGET = 120_000


def _usage(inp, read, create, out=5):
    return {"input_tokens": inp, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": create, "output_tokens": out}


def _replay(events):
    """Feed stream-json events through the real `Brain._handle` into one
    turn, as the reader does, and hand back the finished turn. Preceded by
    the CLI's echo of the turn's message, as the live stream always is:
    before it, nothing is the turn's (tests/test_wake_echo.py)."""
    b = brain.Brain(brain.BrainConfig(home=Path("unused")))
    proc = object()
    b._proc = proc
    t = b._claude_turn("user", None, proc)
    b._inflight = t
    b._handle({"type": "user", "isReplay": True, "uuid": t.tag,
               "message": {"role": "user", "content": "..."}}, proc)
    for ev in events:
        b._handle(ev, proc)
    return t


def _assistant(n, usage, content=None, parent=None):
    return {"type": "assistant", "parent_tool_use_id": parent,
            "message": {"id": f"msg_{n}", "role": "assistant",
                        "content": content or [{"type": "text", "text": "."}],
                        "usage": usage}}


# Generation 3, 2026-09-25 21:23:43Z, copied out of its session transcript:
# the warm-up, then the one user turn that scheduled its rotation. Four API
# calls; the window grew from 99,268 to 101,992.
GEN3_WARMUP = _usage(2, 77_040, 22_031)
GEN3_CALLS = [_usage(2, 99_071, 195), _usage(2, 99_266, 862),
              _usage(2, 100_128, 876), _usage(2, 101_004, 986)]
GEN3_RESULT = {k: sum(c[k] for c in GEN3_CALLS) for k in GEN3_CALLS[0]}
GEN3_RESULT["iterations"] = [dict(GEN3_CALLS[-1], type="message")]


def test_the_live_turn_that_rotated_generation_3_measures_its_real_window():
    """Replayed through the reader: the turn is its LAST call's prompt,
    101,992 tokens, of which 2,919 are conversation. The old measure summed
    all four calls to 399,477; less its 77,042 floor that is 322,435 — the
    exact number in the log that scheduled the rotation."""
    warm = _replay([_assistant(0, GEN3_WARMUP),
                    {"type": "result", "subtype": "success", "usage": GEN3_WARMUP}])
    floor = warm.context_tokens()
    assert floor == 99_073, "the warm-up's prompt is all three columns"

    events = []
    for i, call in enumerate(GEN3_CALLS):
        events.append(_assistant(i + 1, call))
    events.append({"type": "result", "subtype": "success", "usage": GEN3_RESULT})
    turn = _replay(events)

    assert turn.context_tokens() == 101_992
    assert turn.context_tokens() - floor == 2_919
    assert turn.context_tokens() - floor < LIVE_BUDGET, "this turn must not rotate"
    assert turn.result(None).context_tokens == 101_992


def test_the_result_alone_is_read_through_its_last_iteration_not_its_sum():
    """A stream that carried no per-call usage on its assistant events still
    names its last call, under `usage.iterations`."""
    turn = _replay([{"type": "result", "subtype": "success", "usage": GEN3_RESULT}])
    assert turn.context_tokens() == 101_992


def test_a_single_call_result_with_no_breakdown_is_its_own_window():
    """The oldest shape: no per-call usage, no iterations. One call's usage
    IS its prompt — and a single call is the only case where the result is
    not a sum."""
    turn = _replay([{"type": "result", "subtype": "success",
                     "usage": _usage(2, 77_040, 22_031)}])
    assert turn.context_tokens() == 99_073


def test_a_subagents_calls_are_not_the_brains_window():
    """An assistant event with a `parent_tool_use_id` is a sub-agent's call,
    measured against the sub-agent's own, separate context. It must not
    stand in for the brain's."""
    turn = _replay([
        _assistant(1, _usage(2, 99_071, 195)),
        _assistant(2, _usage(2, 500, 40_000), parent="toolu_task"),
        {"type": "result", "subtype": "success",
         "usage": _usage(4, 99_571, 40_195)},
    ])
    assert turn.context_tokens() == 99_268


def test_a_cache_miss_and_a_cache_hit_on_the_same_prompt_measure_the_same():
    """The rule this file replaces said cache_creation was "the cache being
    rebuilt out of the same prompt, not extra tokens in the window", and so
    measured a cache-miss turn as 500 tokens. It is the other way round: the
    same prompt is reported as WRITTEN on a miss and READ on a hit, and the
    window is the same size either way only if both columns count."""
    miss = _replay([{"type": "result", "subtype": "success",
                     "usage": _usage(500, 0, 29_000)}])
    hit = _replay([{"type": "result", "subtype": "success",
                    "usage": _usage(500, 29_000, 0)}])
    assert miss.context_tokens() == hit.context_tokens() == 29_500


# ── end to end, through a real process ─────────────────────────────────

@pytest.fixture
def live_shape(monkeypatch):
    monkeypatch.setenv("FAKE_BRAIN_FLOOR", str(LIVE_FLOOR))


@pytest.mark.asyncio
async def test_a_tool_using_turn_right_after_a_rotation_does_not_schedule_another(
        tmp_path, live_shape):
    """The loop itself. A new generation is born holding only its floor and
    a handover; its first turn uses tools, as nearly every live turn did.
    That turn added under 10,000 tokens and must not schedule a rotation."""
    b = brain.Brain(_config(tmp_path, context_budget=LIVE_BUDGET))
    try:
        await b.start()
        assert await b.rotate(handover="staged the MARK-333 note") is True
        assert b.generation == 2

        result = await b.turn("CALLS:4 send the approved note")

        assert result.stop_reason == "result"
        assert b.rotation_pending is False, (
            f"rotated again straight after a rotation: conversation="
            f"{b.conversation_tokens} floor={b.baseline_tokens} "
            f"context={b.context_tokens}")
        assert b.conversation_tokens < 10_000
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_a_run_of_tool_using_turns_stays_on_one_generation(tmp_path, live_shape):
    """Five tool-heavy turns, ~9,000 tokens of real growth each: 45,000 of a
    120,000 budget. One generation throughout, and the measure climbs by the
    conversation, not by whole copies of the prompt."""
    b = brain.Brain(_config(tmp_path, context_budget=LIVE_BUDGET))
    try:
        await b.start()
        seen = []
        for i in range(5):
            await b.turn(f"CALLS:{2 + i % 4} turn {i}")
            seen.append(b.conversation_tokens)
            assert b.rotation_pending is False, f"turn {i}: {seen}"
        assert b.generation == 1
        assert seen == sorted(seen) and seen[-1] < 50_000, seen
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_a_cold_cache_warmup_measures_the_whole_floor(tmp_path, monkeypatch):
    """Generation 1 of 2026-09-25 booted on a cold cache and logged
    `ctx=2`: its warm-up wrote 98,984 tokens to the cache and read none, and
    only the 2 uncached ones were counted. Every later turn then read that
    floor back as if it were conversation."""
    monkeypatch.setenv("FAKE_BRAIN_FLOOR", str(LIVE_FLOOR))
    monkeypatch.setenv("FAKE_BRAIN_COLD_WARMUP", "1")
    b = brain.Brain(_config(tmp_path, context_budget=50_000))
    try:
        await b.start()
        assert b.baseline_tokens == 10 + 9_000 + LIVE_FLOOR + 1_000, (
            "the cold warm-up's floor is the whole prompt it wrote")

        await b.turn("hello")

        assert b.conversation_tokens == 9_000
        assert b.rotation_pending is False
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_a_turn_that_really_outgrows_the_budget_still_rotates(tmp_path, live_shape):
    """The fix must not simply stop rotation. Real growth past the budget
    schedules it exactly as before."""
    b = brain.Brain(_config(tmp_path, context_budget=20_000))
    try:
        await b.start()
        await b.turn("CALLS:3 first")                # ~9,600 of growth
        assert b.rotation_pending is False
        await b.turn("CALLS:3 second")               # ~18,600
        assert b.rotation_pending is False
        await b.turn("CALLS:3 third")                # ~27,600: over
        assert b.rotation_pending is True
    finally:
        await b.stop()


# ── the model's window, and a floor that nearly fills it ───────────────

@pytest.mark.asyncio
async def test_the_models_window_is_read_off_the_result(tmp_path, live_shape):
    b = brain.Brain(_config(tmp_path, context_budget=LIVE_BUDGET))
    try:
        assert b.context_window is None, "nothing is known before the warm-up"
        await b.start()
        assert b.context_window == 1_000_000
        assert b.effective_context_budget == LIVE_BUDGET, (
            "a 1M window leaves the configured budget alone")
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_a_small_window_caps_the_budget_below_the_configured_one(
        tmp_path, monkeypatch):
    """With a 200k model the live floor plus a 120k budget is 220k: the CLI
    would compact on its own long before JARVIS rotated, and the handover
    would never be written. The budget is capped so that floor + budget
    stays inside the share of the window rotation is allowed to use."""
    monkeypatch.setenv("FAKE_BRAIN_FLOOR", str(LIVE_FLOOR))
    monkeypatch.setenv("FAKE_BRAIN_CONTEXT_WINDOW", "200000")
    b = brain.Brain(_config(tmp_path, context_budget=LIVE_BUDGET))
    try:
        await b.start()
        expected = int(200_000 * brain.ROTATION_WINDOW_SHARE) - b.baseline_tokens
        assert b.effective_context_budget == expected < LIVE_BUDGET

        turns = 0
        while not b.rotation_pending:
            await b.turn(f"turn {turns}")
            turns += 1
            assert turns < 20, "never rotated"
        assert b.conversation_tokens >= expected
        assert b.conversation_tokens < LIVE_BUDGET, (
            "rotated on the capped budget, not the configured one")
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_a_floor_that_fills_the_window_does_not_rotate_every_turn(
        tmp_path, monkeypatch, caplog):
    """If the floor alone takes the window, no rotation can help — a new
    generation starts with the same floor. The budget is held at a minimum
    rather than collapsing to zero, which would rotate after every turn, and
    the operator is told why."""
    monkeypatch.setenv("FAKE_BRAIN_FLOOR", str(LIVE_FLOOR))
    monkeypatch.setenv("FAKE_BRAIN_CONTEXT_WINDOW", "120000")
    b = brain.Brain(_config(tmp_path, context_budget=LIVE_BUDGET))
    try:
        with caplog.at_level(logging.WARNING, logger="jarvis.brain"):
            await b.start()
        assert b.effective_context_budget == brain.ROTATION_MIN_BUDGET
        assert any("floor" in r.getMessage() and "window" in r.getMessage()
                   for r in caplog.records), [r.getMessage() for r in caplog.records]

        await b.turn("CALLS:3 hello")
        assert b.rotation_pending is False
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_the_rotation_log_line_says_what_was_measured(tmp_path, live_shape, caplog):
    """The loop ran for five hours behind a log line that could not tell a
    sum from a window. The line now carries the window, the floor, the
    model's window and how many calls the turn made."""
    b = brain.Brain(_config(tmp_path, context_budget=5_000))
    try:
        await b.start()
        with caplog.at_level(logging.INFO, logger="jarvis.brain"):
            await b.turn("CALLS:3 hello")
        line = next(r.getMessage() for r in caplog.records
                    if "rotation scheduled" in r.getMessage())
        for part in ("conversation=", "budget=", "floor=", "context=",
                     "window=1000000", "calls=3"):
            assert part in line, line
    finally:
        await b.stop()


# ── the review's findings (2026-09-26) ─────────────────────────────────

@pytest.mark.asyncio
async def test_a_crash_restart_does_not_inherit_a_pending_rotation(tmp_path, live_shape):
    """A rotation is scheduled against a generation's conversation. If that
    process dies before the rotation happens, the one that replaces it has
    none: carrying the flag over rotated a fresh generation at its first
    pause, with no handover of its own — a rotation no conversation caused."""
    from tests.test_brain import _wait_until
    b = brain.Brain(_config(tmp_path, context_budget=5_000))
    try:
        await b.start()
        await b.turn("CALLS:2 over the budget")
        assert b.rotation_pending is True

        await b.turn("DIE")                          # the process exits mid-turn
        assert await _wait_until(lambda: b.ready and b.generation == 2, 15.0), (
            "the brain did not come back")

        assert b.rotation_pending is False, "the new process has had no conversation"
        assert b.turns_since_rotation == 0
        assert b.conversation_tokens == 0
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_a_turn_that_ends_in_error_is_still_measured(tmp_path, live_shape):
    """Every call of a turn that FAILED still reported its own prompt. A turn
    that grew the window with tool results and then failed — an overloaded
    API, or a prompt grown too long — used to be thrown away unmeasured, so
    the one thing that could shrink the window was never scheduled."""
    b = brain.Brain(_config(tmp_path, context_budget=5_000))
    try:
        await b.start()
        result = await b.turn("CALLSERR:3 read three big things")

        assert result.stop_reason == "error"
        assert b.context_tokens == 10 + 18_000 + 600 + LIVE_FLOOR + 1_000, (
            "the window is the last real call's prompt, not the synthetic error")
        assert b.rotation_pending is True
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_an_error_with_no_real_call_changes_nothing(tmp_path, live_shape):
    """An error that never reached the model (the CLI's synthetic message,
    no prompt at all) is not a measurement: the window stays as it was."""
    b = brain.Brain(_config(tmp_path, context_budget=LIVE_BUDGET))
    try:
        await b.start()
        before = b.context_tokens
        result = await b.turn("APIERROR")
        assert result.stop_reason == "error"
        assert b.context_tokens == before
        assert b.rotation_pending is False
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_a_failed_rotation_puts_back_what_the_old_process_said_about_itself(
        tmp_path, monkeypatch):
    """The replacement's init event rewrites the session id, the model and
    the MCP roster before its warm-up fails. The old process goes on
    serving, so what JARVIS reports about itself ("what are you connected
    to?") must be the old process's again."""
    b = brain.Brain(_config(tmp_path, context_budget=LIVE_BUDGET))
    try:
        await b.start()
        before = (b.session_id, b.model_in_use, list(b.mcp_servers), list(b.live_tools),
                  b.context_tokens, b.baseline_tokens, b.context_window)

        async def stillborn(rotating=False):
            b.generation += 1
            b.session_id, b.model_in_use = "replacement", "some-other-model"
            b.mcp_servers = [{"name": "ghost", "status": "failed"}]
            b.live_tools = ["mcp__ghost__probe"]
            return False

        monkeypatch.setattr(b, "_spawn_locked", stillborn)
        assert await b.rotate(handover="x") is False

        after = (b.session_id, b.model_in_use, list(b.mcp_servers), list(b.live_tools),
                 b.context_tokens, b.baseline_tokens, b.context_window)
        assert after == before
        assert b.generation == 1 and b.ready
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_one_message_sent_as_several_events_is_one_call(tmp_path, live_shape):
    """Under --include-partial-messages the CLI sends a message with a text
    block and a tool_use as two `assistant` events with one id. That is one
    API call; the turn made two, not three."""
    b = brain.Brain(_config(tmp_path, context_budget=LIVE_BUDGET))
    try:
        await b.start()
        await b.turn("SPLIT look first")
        assert b.last_turn_calls == 2
        assert b.context_tokens == 10 + 18_000 + 300 + LIVE_FLOOR + 1_000
    finally:
        await b.stop()
