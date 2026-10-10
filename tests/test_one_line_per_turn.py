"""A turn that uses a tool says one thing, at the end.

Everything the brain writes is spoken the instant it is written, and it
narrates around its tools. Observed live, for a single instruction to pass a
message to a session:

    "Will say that to the session."     -> spoken
        steer_session
    "Saying this to it now."            -> spoken
        steer_session
    "Passed that to chitauri, sir."      -> spoken

Three sentences saying the same thing, all of which the user had to sit
through. The persona forbids it and he complies for a while and then does not,
so it is enforced in the server instead.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import server


def spoken(script, hold_for=0.6):
    """Run a turn's emissions through the gate; return what reaches the mouth.

    `script` is a list of ("text", s) and ("tool",) in the order the CLI
    reports them.
    """
    out = []
    gate = server._OneLinePerTurn(out.append, hold_for=hold_for)
    for step in script:
        if step[0] == "tool":
            gate.tool_started()
        else:
            gate.delta(step[1])
    gate.finish()
    return out


def test_the_reported_failure_is_one_sentence_now():
    """The exact sequence from the log above."""
    assert spoken([
        ("text", "Will say that to the session."),
        ("tool",),
        ("text", "Saying this to it now."),
        ("tool",),
        ("text", "Passed that to chitauri, sir."),
    ]) == ["Passed that to chitauri, sir."]


def test_narration_before_a_single_tool_is_dropped():
    assert spoken([
        ("text", "Sending it now."),
        ("tool",),
        ("text", "Sent to chitauri, sir."),
    ]) == ["Sent to chitauri, sir."]


def test_a_tool_that_produces_no_report_says_nothing():
    """Silence beats announcing an intention he never reported on."""
    assert spoken([("text", "Looking now."), ("tool",)]) == []


def test_ordinary_conversation_is_untouched():
    """No tool: the hold expires and the turn streams as it always did."""
    assert "".join(spoken([("text", "Evening, "), ("text", "sir.")],
                          hold_for=0.0)) == "Evening, sir."


def test_a_long_answer_still_streams_when_no_tool_is_used():
    """The hold covers the opening only. Once released, deltas go straight
    out — a spoken answer must not wait for the whole turn to generate."""
    out = []
    gate = server._OneLinePerTurn(out.append, hold_for=0.0)
    gate.delta("First. ")           # releases the hold
    gate.delta("Second. ")
    gate.delta("Third.")
    gate.finish()
    assert len(out) >= 2, "later deltas should not have been buffered"
    assert "".join(out) == "First. Second. Third."


def test_nothing_written_at_all_is_silence_not_an_empty_utterance():
    assert spoken([]) == []
    assert spoken([("text", "   ")]) == []


def test_a_second_round_of_narration_cannot_slip_out_between_tools():
    """The bug in the first attempt at this: the gate reopened after the
    first tool, so narration before the SECOND tool was still spoken."""
    assert spoken([
        ("tool",),
        ("text", "Right, one moment."),
        ("tool",),
        ("text", "Done, sir."),
    ]) == ["Done, sir."]


import asyncio
import pytest


@pytest.mark.asyncio
async def test_the_hold_releases_on_its_own_clock_not_on_the_next_delta():
    """Measured 2026-09-24 on the latency line: first_cut equalled the turn's
    whole duration on every turn. The CLI delivers a short reply in one or
    two partial messages inside the hold window, and the release was only
    checked when ANOTHER delta arrived, so a reply that had fully arrived
    sat in the hold until the turn ended, about a second later. The hold is
    a timer: what it holds goes out when the window closes, delta or no
    delta."""
    out = []
    gate = server._OneLinePerTurn(out.append, hold_for=0.05)
    gate.delta("Evening, sir. All quiet on the sessions.")
    assert out == [], "held for the window"
    await asyncio.sleep(0.2)
    assert out == ["Evening, sir. All quiet on the sessions."], "released by the clock, before finish()"
    gate.delta(" Nothing waiting on you.")
    assert out[-1] == " Nothing waiting on you.", "and streaming from then on"
    gate.finish()


@pytest.mark.asyncio
async def test_a_tool_inside_the_window_still_bins_the_narration():
    out = []
    gate = server._OneLinePerTurn(out.append, hold_for=0.05)
    gate.delta("Will say that to the session.")
    gate.tool_started()
    await asyncio.sleep(0.2)
    assert out == [], "the timer must not release what a tool has already binned"
    gate.delta("Passed that to chitauri, sir.")
    gate.finish()
    assert out == ["Passed that to chitauri, sir."]
