"""Every decision the gate makes is written down before the call resolves.

Until now the only trace of a tool call anywhere was one line:

    log.info("latency: ... tools=%s", result.tools)

emitted after the turn had finished speaking, through a `basicConfig` with
no file handler (server.py) — so it went to stderr and left with the
scrollback. That is the difference between "he posted twice" and "he posted
twice and nobody could tell", which is the shape of the whole incident this
work came out of.

It also fixes a second bug of the same family. The killed-turn clause
scanned every business action ever recorded for a matching tool name, with
no bound in time, so a post submitted LAST WEEK made a turn that merely
reached for the tool today announce "I had already sent create_post" —
a confident claim from stale evidence, which is what started all this.
"""

import importlib
import time

import pytest


@pytest.fixture
def log(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import data_paths
    importlib.reload(data_paths)
    import tool_log
    importlib.reload(tool_log)
    tool_log.init_db()
    return tool_log


def test_a_decision_is_written_and_can_be_read_back(log):
    log.record(tool="mcp__linkedin__create_post", server="linkedin",
               decision="deny", reason="staged", digest="abc", tool_use_id="t1")
    rows = log.recent()
    assert len(rows) == 1
    row = rows[0]
    assert row["tool"] == "mcp__linkedin__create_post"
    assert row["server"] == "linkedin"
    assert row["decision"] == "deny"
    assert row["digest"] == "abc"
    assert row["tool_use_id"] == "t1"
    assert row["at"] > 0


def test_newest_first(log):
    for i in range(5):
        log.record(tool=f"mcp__x__t{i}", server="x", decision="allow", reason="r")
    assert [r["tool"] for r in log.recent()] == [f"mcp__x__t{i}" for i in (4, 3, 2, 1, 0)]


def test_it_answers_what_went_out_in_one_window(log):
    """The question the killed-turn clause actually has: not 'was this tool
    ever allowed' but 'was it allowed during THIS turn'."""
    log.record(tool="mcp__linkedin__create_post", server="linkedin",
               decision="allow", reason="approved long ago")
    old = time.time() - 3600
    log.recent()  # no-op read
    # The row above is stamped with `time.time()` too, and the window query
    # is `at >= since`: taken in the same clock tick — Windows' clock is
    # coarse — `since` equalled that stamp and the "long ago" call sat
    # inside the window. A tick of daylight on BOTH sides of `since`.
    time.sleep(0.02)
    since = time.time()
    time.sleep(0.02)
    log.record(tool="mcp__linkedin__send_message", server="linkedin",
               decision="allow", reason="just now")

    went = log.allowed_since(since)
    assert "mcp__linkedin__send_message" in went
    assert "mcp__linkedin__create_post" not in went, \
        "a call allowed before the window is not something this turn did"
    assert old < since


def test_a_denial_is_not_something_that_went_out(log):
    since = time.time()
    time.sleep(0.02)
    log.record(tool="mcp__linkedin__create_post", server="linkedin",
               decision="deny", reason="staged")
    assert log.allowed_since(since) == set()
    assert log.attempted_since(since) == {"mcp__linkedin__create_post"}


def test_the_log_is_bounded_exactly(log, monkeypatch):
    """It records every call the brain makes, forever, on a machine nobody
    prunes. A log that fills a disk is an outage — and a bound with a fudge
    factor is one nobody can state, so this is the real cap, not the cap
    plus however many arrived since the last sweep."""
    assert log.MAX_ROWS < 100_000, "the shipped cap has to be a real bound"
    monkeypatch.setattr(log, "MAX_ROWS", 50)
    for _ in range(140):
        log.record(tool="mcp__x__t", server="x", decision="allow", reason="r")
    assert log.count() == 50, log.count()


def test_pruning_keeps_the_NEWEST(log, monkeypatch):
    monkeypatch.setattr(log, "MAX_ROWS", 5)
    for i in range(20):
        log.record(tool=f"mcp__x__t{i}", server="x", decision="allow", reason="r")
    assert [r["tool"] for r in log.recent()] == [f"mcp__x__t{i}" for i in (19, 18, 17, 16, 15)]


def test_it_never_raises_at_the_call_site(log, monkeypatch):
    """This runs inside the gate. A store that will not write must not turn
    into an allow, or into a 500 that the hook reads as a failure."""
    def boom(*a, **kw):
        raise OSError("disk is full")
    monkeypatch.setattr(log, "connect", boom)
    log.record(tool="mcp__x__t", server="x", decision="deny", reason="r")
    assert log.recent.__doc__ is not None    # and reading is just as safe
    monkeypatch.setattr(log, "connect", boom)
    assert log.recent() == []
    assert log.allowed_since(0) == set()


def test_a_reason_is_kept_whole_enough_to_be_useful(log):
    long_reason = "x" * 5000
    log.record(tool="mcp__x__t", server="x", decision="deny", reason=long_reason)
    kept = log.recent()[0]["reason"]
    assert 200 <= len(kept) <= 1000, len(kept)


def test_a_tool_allowed_once_and_refused_once_is_both(log):
    """Derived as "attempted minus allowed", the refusal disappears. One
    turn doing both is the incident this whole area came from, so the two
    questions are asked separately."""
    since = time.time()
    time.sleep(0.02)
    log.record(tool="mcp__linkedin__create_post", server="linkedin",
               decision="allow", reason="approved")
    log.record(tool="mcp__linkedin__create_post", server="linkedin",
               decision="deny", reason="staged")
    assert "mcp__linkedin__create_post" in log.allowed_since(since)
    assert "mcp__linkedin__create_post" in log.denied_since(since)
    assert log.attempted_since(since) - log.allowed_since(since) == set(), \
        "which is exactly why denied_since is asked rather than derived"
