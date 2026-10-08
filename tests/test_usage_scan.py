"""usage_scan: per-session token usage, read off the CLI's own transcripts.

The rules these tests defend:

  * A number that has not been measured is never rendered as zero. A session
    with no transcript is absent from the report, and a report with nothing
    to read says `measured is False`.
  * The two config roots are HARDLINKED on the live machine — the same inode
    appears under `~/.claude` and `~/.claude-orcha`. Counting the union of
    the two roots naively doubles every token on the page.
  * JARVIS's own machinery (his brain, and the one-shot runs he spawns) is
    not the user's work and is never added into the user's totals.
  * A subagent's tokens belong to the conversation that dispatched it, but
    they are kept apart from what the conversation itself spent.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import usage_scan as us
from tests.fixtures.transcripts import (
    append_turns, encode, write_agent_sidecar, write_agent_transcript,
    write_transcript,
)

# A fixed clock, so "today" and "active" are decidable in a test.
NOW = 1788404571.0
HOUR = 3600.0
DAY = 86400.0


def roots(tmp_path):
    return [tmp_path / ".claude", tmp_path / ".claude-orcha"]


def one(report, session_id):
    return next(s for s in report.sessions if s.session_id == session_id)


# ── nothing to read ─────────────────────────────────────────────────────────

def test_nothing_on_disk_is_not_zero_usage(tmp_path):
    """No transcripts at all is an ABSENCE of measurement, and says so."""
    report = us.report(roots=roots(tmp_path), now=NOW)

    assert report.measured is False
    assert report.sessions == []
    assert report.files == 0


def test_a_missing_root_is_not_an_error(tmp_path):
    write_transcript(tmp_path / ".claude", cwd="/p/one", session_id="s1",
                     turns=[dict(when=NOW - HOUR, out=10)])

    report = us.report(roots=[tmp_path / ".claude", tmp_path / "nope"], now=NOW)

    assert report.measured is True
    assert [s.session_id for s in report.sessions] == ["s1"]


# ── the arithmetic ──────────────────────────────────────────────────────────

def test_tokens_are_summed_over_every_assistant_turn(tmp_path):
    a, _ = roots(tmp_path)
    write_transcript(a, cwd="/p/one", session_id="s1", turns=[
        dict(when=NOW - 2 * HOUR, inp=3, out=100, cache_read=1000, cache_creation=50),
        dict(when=NOW - HOUR, inp=5, out=200, cache_read=2000, cache_creation=60),
    ])

    s = one(us.report(roots=[a], now=NOW), "s1")

    assert s.tokens.input == 8
    assert s.tokens.output == 300
    assert s.tokens.cache_read == 3000
    assert s.tokens.cache_creation == 110
    assert s.tokens.total == 8 + 300 + 3000 + 110
    assert s.turns == 2


def test_a_line_with_no_usage_block_is_not_a_turn(tmp_path):
    """Noise lines (ai-title, attachments, half-written JSON) must neither
    raise nor inflate the turn count."""
    a, _ = roots(tmp_path)
    p = write_transcript(a, cwd="/p/one", session_id="s1",
                         turns=[dict(when=NOW - HOUR, out=7)])
    with open(p, "a") as fh:
        fh.write(json.dumps({"type": "assistant", "sessionId": "s1",
                             "message": {"role": "assistant"}}) + "\n")

    s = one(us.report(roots=[a], now=NOW), "s1")

    assert s.turns == 1 and s.tokens.output == 7


def test_the_context_size_is_the_last_turns_input_not_the_sum(tmp_path):
    """Summed input tokens are cumulative across a conversation; the CONTEXT
    is what one request carried. Showing the sum as "context" would report a
    200k window as several million."""
    a, _ = roots(tmp_path)
    write_transcript(a, cwd="/p/one", session_id="s1", turns=[
        dict(when=NOW - 2 * HOUR, inp=4, cache_read=10_000, cache_creation=500),
        dict(when=NOW - HOUR, inp=6, cache_read=40_000, cache_creation=900),
    ])

    s = one(us.report(roots=[a], now=NOW), "s1")

    assert s.context_tokens == 6 + 40_000 + 900


def test_a_synthetic_turn_does_not_wipe_the_real_context(tmp_path):
    """Found live on 2026-09-03, session 5a0eaa6f: the last line of a busy
    transcript was a `<synthetic>` turn with all four counts at zero, and it
    overwrote a genuine 481k context with `0`. A turn that carried nothing
    was never a request; it is the CLI's own bookkeeping, and it must not be
    allowed to report an empty context window as a measured fact.
    """
    a, _ = roots(tmp_path)
    write_transcript(a, cwd="/p/one", session_id="s1", turns=[
        dict(when=NOW - 2 * HOUR, inp=2, cache_read=478_015, cache_creation=3_181,
             out=63, model="claude-opus-5"),
        dict(when=NOW - HOUR, model="<synthetic>"),
    ])

    s = one(us.report(roots=[a], now=NOW), "s1")

    assert s.context_tokens == 2 + 478_015 + 3_181


def test_a_session_that_never_reported_usage_has_an_unknown_context(tmp_path):
    a, _ = roots(tmp_path)
    write_transcript(a, cwd="/p/one", session_id="s1", turns=[], noise=True)

    report = us.report(roots=[a], now=NOW)

    # A file with no assistant turn is still a session we have READ — but its
    # context is unknown, not zero.
    s = one(report, "s1")
    assert s.context_tokens is None
    assert s.turns == 0


# ── the hardlink trap ───────────────────────────────────────────────────────

def test_the_same_transcript_hardlinked_under_both_roots_is_counted_once(tmp_path):
    """Measured on 2026-09-03: `~/.claude/projects/...` and
    `~/.claude-orcha/projects/...` are the SAME inode (link count 2). A union
    of the two roots that does not dedupe doubles every number on the page."""
    a, b = roots(tmp_path)
    src = write_transcript(a, cwd="/p/one", session_id="s1",
                           turns=[dict(when=NOW - HOUR, out=1000)])
    dst = b / "projects" / encode("/p/one") / "s1.jsonl"
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.link(src, dst)
    assert src.stat().st_ino == dst.stat().st_ino

    report = us.report(roots=[a, b], now=NOW)

    assert len(report.sessions) == 1
    assert one(report, "s1").tokens.output == 1000
    assert report.files == 1


def test_one_file_reached_by_two_names_is_read_once(tmp_path):
    """The dedupe key is the file's IDENTITY, not its name.

    The live hardlinks happen to sit at the same relative path under both
    roots, so a name-based key would dedupe them too — and would then be
    wrong in general and right by luck. This is the general statement: one
    inode, two names, one reading.
    """
    a, b = roots(tmp_path)
    src = write_transcript(a, cwd="/p/one", session_id="s1",
                           turns=[dict(when=NOW - HOUR, out=1000)])
    alias = b / "projects" / "some-other-dir" / "s2.jsonl"
    alias.parent.mkdir(parents=True, exist_ok=True)
    os.link(src, alias)

    report = us.report(roots=[a, b], now=NOW)

    assert [s.session_id for s in report.sessions] == ["s1"]
    assert report.totals.output == 1000
    assert report.files == 1


def test_two_genuinely_different_transcripts_are_both_counted(tmp_path):
    """The dedupe must key on the inode, not on the file's name — two roots
    can legitimately hold different conversations."""
    a, b = roots(tmp_path)
    write_transcript(a, cwd="/p/one", session_id="s1",
                     turns=[dict(when=NOW - HOUR, out=10)])
    write_transcript(b, cwd="/p/two", session_id="s2",
                     turns=[dict(when=NOW - HOUR, out=20)])

    report = us.report(roots=[a, b], now=NOW)

    assert {s.session_id for s in report.sessions} == {"s1", "s2"}
    assert report.totals.output == 30


# ── subagents ───────────────────────────────────────────────────────────────

def test_a_subagents_tokens_belong_to_its_session_but_are_kept_apart(tmp_path):
    a, _ = roots(tmp_path)
    write_transcript(a, cwd="/p/one", session_id="s1",
                     turns=[dict(when=NOW - HOUR, out=100)])
    write_agent_transcript(a, cwd="/p/one", session_id="s1",
                           agent_id="a1", prompt="find the bug",
                           turns=[dict(when=NOW - 30 * 60, out=400,
                                       model="claude-opus-5")])

    s = one(us.report(roots=[a], now=NOW), "s1")

    assert s.tokens.output == 100, "the conversation's own spend"
    assert s.agent_tokens.output == 400, "what it dispatched"
    assert s.total_tokens.output == 500
    agent, = s.agents
    assert agent.agent_id == "a1"
    assert agent.model == "claude-opus-5"
    assert agent.prompt.startswith("find the bug")


def test_an_agents_type_and_description_come_from_its_sidecar(tmp_path):
    """Beside every `agent-<id>.jsonl` the CLI writes an `agent-<id>.json`
    holding `agentType`, a one-line `description` and `spawnDepth`. That
    file is the only place a subagent says WHAT IT IS — the transcript
    carries the full prompt and nothing else — and a nested agent
    (spawnDepth > 1) is only visible from it.
    """
    a, _ = roots(tmp_path)
    write_agent_transcript(a, cwd="/p/one", session_id="s1", agent_id="a1",
                           prompt="a very long brief " * 40,
                           turns=[dict(when=NOW - 10, out=5)])
    write_agent_sidecar(a, cwd="/p/one", session_id="s1", agent_id="a1",
                        agent_type="Explore", description="Find the bug",
                        parent_agent_id="a0", spawn_depth=2)

    agent, = one(us.report(roots=[a], now=NOW), "s1").agents

    assert agent.agent_type == "Explore"
    assert agent.description == "Find the bug"
    assert agent.depth == 2
    assert agent.parent_agent_id == "a0"


def test_a_subagent_with_no_sidecar_still_reports_its_tokens(tmp_path):
    """Older transcripts have no sidecar. The unknown fields come back empty
    — never guessed, and never a reason to drop the agent."""
    a, _ = roots(tmp_path)
    write_agent_transcript(a, cwd="/p/one", session_id="s1", agent_id="a1",
                           turns=[dict(when=NOW - 10, out=5)])

    agent, = one(us.report(roots=[a], now=NOW), "s1").agents

    assert agent.agent_type == "" and agent.description == ""
    assert agent.depth == 0
    assert agent.tokens.output == 5


def test_a_corrupt_sidecar_is_ignored_not_raised(tmp_path):
    a, _ = roots(tmp_path)
    write_agent_transcript(a, cwd="/p/one", session_id="s1", agent_id="a1",
                           turns=[dict(when=NOW - 10, out=5)])
    side = (a / "projects" / encode("/p/one") / "s1" / "subagents"
            / "agent-a1.meta.json")
    side.write_text("{half writ")

    agent, = one(us.report(roots=[a], now=NOW), "s1").agents

    assert agent.agent_type == "" and agent.tokens.output == 5


def test_a_sidecar_is_never_mistaken_for_a_transcript(tmp_path):
    """`agent-x.meta.json` and `agent-x.jsonl` are ONE agent, and only the
    `.jsonl` carries usage. Reading the sidecar as a transcript would list a
    phantom agent with zero tokens beside every real one — so this checks
    the agent that comes back is the one holding the tokens, not just that
    exactly one came back."""
    a, _ = roots(tmp_path)
    write_agent_transcript(a, cwd="/p/one", session_id="s1", agent_id="a1",
                           turns=[dict(when=NOW - 10, out=5)])
    write_agent_sidecar(a, cwd="/p/one", session_id="s1", agent_id="a1")

    s = one(us.report(roots=[a], now=NOW), "s1")

    assert [a.agent_id for a in s.agents] == ["a1"]
    assert s.agents[0].tokens.output == 5
    assert s.agent_tokens.output == 5


def test_a_subagent_written_within_the_active_window_reads_as_running(tmp_path):
    """Files are all we have: a subagent whose transcript was written seconds
    ago is working, one last written an hour ago is not. This is stated as
    "active" against a stated window, never as a live process check."""
    a, _ = roots(tmp_path)
    write_transcript(a, cwd="/p/one", session_id="s1",
                     turns=[dict(when=NOW - HOUR, out=1)])
    write_agent_transcript(a, cwd="/p/one", session_id="s1", agent_id="live",
                           turns=[dict(when=NOW - 5, out=10)])
    write_agent_transcript(a, cwd="/p/one", session_id="s1", agent_id="done",
                           turns=[dict(when=NOW - HOUR, out=10)])

    s = one(us.report(roots=[a], now=NOW), "s1")

    by_id = {ag.agent_id: ag for ag in s.agents}
    assert by_id["live"].active is True
    assert by_id["done"].active is False
    assert s.active_agents == 1


def test_a_subagent_folder_with_no_conversation_file_is_still_reported(tmp_path):
    """The parent transcript can be missing (pruned, or under the other root
    only). Dropping the agents would silently lose their tokens."""
    a, _ = roots(tmp_path)
    write_agent_transcript(a, cwd="/p/one", session_id="s1", agent_id="a1",
                           turns=[dict(when=NOW - HOUR, out=42)])

    s = one(us.report(roots=[a], now=NOW), "s1")

    assert s.turns == 0 and s.tokens.output == 0
    assert s.agent_tokens.output == 42
    assert s.cwd == "/p/one"


# ── incremental reading ─────────────────────────────────────────────────────

def test_an_appended_transcript_is_read_only_from_where_it_left_off(tmp_path):
    """A 95 MB transcript cannot be re-read on every request. Appended bytes
    only, and the total must still be right."""
    a, _ = roots(tmp_path)
    p = write_transcript(a, cwd="/p/one", session_id="s1",
                         turns=[dict(when=NOW - 2 * HOUR, out=100)])
    cache = us.Cache()

    first = us.report(roots=[a], now=NOW, cache=cache)
    grew_by = p.stat().st_size
    append_turns(p, session_id="s1", cwd="/p/one",
                 turns=[dict(when=NOW - HOUR, out=250)])
    grew_by = p.stat().st_size - grew_by
    second = us.report(roots=[a], now=NOW, cache=cache)

    assert first.bytes_read == p.stat().st_size - grew_by
    assert second.bytes_read == grew_by
    assert one(second, "s1").tokens.output == 350
    assert one(second, "s1").turns == 2


def test_an_unchanged_transcript_is_not_read_again(tmp_path):
    a, _ = roots(tmp_path)
    write_transcript(a, cwd="/p/one", session_id="s1",
                     turns=[dict(when=NOW - HOUR, out=100)])
    cache = us.Cache()

    us.report(roots=[a], now=NOW, cache=cache)
    again = us.report(roots=[a], now=NOW, cache=cache)

    assert again.bytes_read == 0
    assert one(again, "s1").tokens.output == 100


def test_a_transcript_that_shrank_is_read_from_the_start_again(tmp_path):
    """If a file is ever rewritten rather than appended to, resuming from the
    old offset would carry a total that no longer describes the file."""
    a, _ = roots(tmp_path)
    p = write_transcript(a, cwd="/p/one", session_id="s1", turns=[
        dict(when=NOW - 2 * HOUR, out=100), dict(when=NOW - HOUR, out=100)])
    cache = us.Cache()
    assert one(us.report(roots=[a], now=NOW, cache=cache), "s1").tokens.output == 200

    write_transcript(a, cwd="/p/one", session_id="s1",
                     turns=[dict(when=NOW - HOUR, out=7)])

    assert one(us.report(roots=[a], now=NOW, cache=cache), "s1").tokens.output == 7


def test_a_replaced_file_at_the_same_path_and_size_is_read_from_the_start(tmp_path):
    """Same path, same length, different inode — a rotation. Size alone would
    call that unchanged and report the old file's totals forever."""
    a, _ = roots(tmp_path)
    p = write_transcript(a, cwd="/p/one", session_id="s1",
                         turns=[dict(when=NOW - HOUR, out=111)])
    cache = us.Cache()
    us.report(roots=[a], now=NOW, cache=cache)

    body = p.read_text().replace('"output_tokens": 111', '"output_tokens": 222')
    p.unlink()
    p.write_text(body, newline="")   # byte-for-byte: no CRLF on Windows
    assert len(body) == p.stat().st_size

    assert one(us.report(roots=[a], now=NOW, cache=cache), "s1").tokens.output == 222


# ── slicing ─────────────────────────────────────────────────────────────────

def test_usage_is_bucketed_by_the_lines_own_timestamp(tmp_path):
    """mtime describes the whole file; the turn's own timestamp describes the
    turn. A conversation touched today must not push last week's tokens into
    today's bucket."""
    a, _ = roots(tmp_path)
    write_transcript(a, cwd="/p/one", session_id="s1", turns=[
        dict(when=NOW - 3 * DAY, out=10),
        dict(when=NOW - HOUR, out=20),
        dict(when=NOW - 2 * HOUR, out=5),
    ])

    report = us.report(roots=[a], now=NOW)

    days = {d.day: d.tokens.output for d in report.daily}
    assert days[us.day_key(NOW)] == 25
    assert days[us.day_key(NOW - 3 * DAY)] == 10
    assert report.today.output == 25


def test_per_model_totals_are_kept_apart(tmp_path):
    a, _ = roots(tmp_path)
    write_transcript(a, cwd="/p/one", session_id="s1", turns=[
        dict(when=NOW - HOUR, out=10, model="claude-sonnet-5"),
        dict(when=NOW - HOUR, out=90, model="claude-opus-5"),
    ])

    report = us.report(roots=[a], now=NOW)

    assert report.models["claude-opus-5"].output == 90
    assert report.models["claude-sonnet-5"].output == 10
    assert one(report, "s1").models["claude-opus-5"].output == 90


def test_sessions_come_back_most_recently_active_first(tmp_path):
    a, _ = roots(tmp_path)
    write_transcript(a, cwd="/p/one", session_id="old",
                     turns=[dict(when=NOW - 5 * DAY, out=1)])
    write_transcript(a, cwd="/p/two", session_id="new",
                     turns=[dict(when=NOW - 60, out=1)])

    report = us.report(roots=[a], now=NOW)

    assert [s.session_id for s in report.sessions] == ["new", "old"]


def test_a_worktree_cwd_is_named_for_its_repo_not_its_branch(tmp_path):
    """Same rule as session_watch.project_name — the two views must agree on
    what a project is called or a user cannot line them up."""
    a, _ = roots(tmp_path)
    cwd = "/Users/e/Projects/jarvis/.claude/worktrees/runs-dashboard"
    write_transcript(a, cwd=cwd, session_id="s1",
                     turns=[dict(when=NOW - HOUR, out=1)])

    assert one(us.report(roots=[a], now=NOW), "s1").project == "jarvis"


# ── JARVIS's own machinery is not the user's work ───────────────────────────

def test_a_spawned_runs_transcript_is_reported_apart_from_the_users_work(tmp_path):
    """A run's id IS its Claude Code session id (`--session-id`), so a run's
    transcript looks exactly like a conversation. It is JARVIS's own
    machinery and must never swell the user's totals."""
    a, _ = roots(tmp_path)
    write_transcript(a, cwd="/p/one", session_id="user-session",
                     turns=[dict(when=NOW - HOUR, out=10)])
    write_transcript(a, cwd="/p/one", session_id="run-id-1",
                     turns=[dict(when=NOW - HOUR, out=990)])

    report = us.report(roots=[a], now=NOW, own_session_ids={"run-id-1"})

    assert [s.session_id for s in report.sessions] == ["user-session"]
    assert report.totals.output == 10
    assert report.own_totals.output == 990
    assert [s.session_id for s in report.own_sessions] == ["run-id-1"]


def test_the_brains_own_transcript_is_never_the_users_work(tmp_path, monkeypatch):
    """The brain is a Claude Code session with a transcript like any other.
    It is identified the same way session_watch identifies it: by its cwd."""
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    a, _ = roots(tmp_path)
    brain_cwd = str(tmp_path / "data" / "jarvis")
    write_transcript(a, cwd=brain_cwd, session_id="brain",
                     turns=[dict(when=NOW - HOUR, out=500)])
    write_transcript(a, cwd="/p/one", session_id="mine",
                     turns=[dict(when=NOW - HOUR, out=10)])

    report = us.report(roots=[a], now=NOW)

    assert [s.session_id for s in report.sessions] == ["mine"]
    assert report.totals.output == 10
    assert report.own_totals.output == 500


def test_the_days_buckets_count_the_users_work_only(tmp_path):
    a, _ = roots(tmp_path)
    write_transcript(a, cwd="/p/one", session_id="mine",
                     turns=[dict(when=NOW - HOUR, out=10)])
    write_transcript(a, cwd="/p/one", session_id="run-1",
                     turns=[dict(when=NOW - HOUR, out=999)])

    report = us.report(roots=[a], now=NOW, own_session_ids={"run-1"})

    assert report.today.output == 10


# ── the shape the dashboard reads ───────────────────────────────────────────

def test_the_json_shape_carries_every_number_it_shows_and_no_others(tmp_path):
    a, _ = roots(tmp_path)
    write_transcript(a, cwd="/p/one", session_id="s1",
                     turns=[dict(when=NOW - HOUR, inp=1, out=2,
                                 cache_read=3, cache_creation=4)])
    write_agent_transcript(a, cwd="/p/one", session_id="s1", agent_id="a1",
                           turns=[dict(when=NOW - 10, out=5)])

    body = us.snapshot(roots=[a], now=NOW)

    assert body["measured"] is True
    assert body["scanned_at"] == NOW
    assert body["active_within_sec"] == us.ACTIVE_WITHIN_SEC
    s, = body["sessions"]
    assert s["session_id"] == "s1" and s["project"] == "one"
    assert s["tokens"] == {"input": 1, "output": 2, "cache_read": 3,
                           "cache_creation": 4, "total": 10}
    assert s["agent_tokens"]["output"] == 5
    assert s["context_tokens"] == 1 + 3 + 4
    assert s["agents"][0]["agent_id"] == "a1"
    assert s["agents"][0]["active"] is True


# ── the truncation the caption talks about ─────────────────────────────────
#
# The page ranks what it is given by SPEND and says "N smaller conversations
# not listed". `snapshot` truncated by RECENCY, so the unlisted ones were
# merely OLDER — a caption asserting a thousand conversations were all
# smaller than the twenty-five shown, off a sort that never compared them.
# The file's own reason for ranking by spend is a 2-billion-token
# conversation buried under a dozen one-turn ones; that reason applies to
# the truncation too, or the page never sees it.

def test_the_biggest_spender_survives_the_truncation(tmp_path):
    a, _ = roots(tmp_path)
    # One enormous, ancient conversation and a crowd of recent tiny ones.
    write_transcript(a, cwd="/p/big", session_id="the-expensive-one",
                     turns=[dict(when=NOW - 300 * DAY, out=2_000_000)])
    for i in range(us.MAX_SESSIONS + 10):
        write_transcript(a, cwd=f"/p/small{i}", session_id=f"tiny{i:03d}",
                         turns=[dict(when=NOW - i, out=1)])

    body = us.snapshot(roots=[a], now=NOW)

    ids = [s["session_id"] for s in body["sessions"]]
    assert "the-expensive-one" in ids, (
        "the page says 'largest first' about a list the largest is not in")


def test_the_payload_says_how_many_of_the_largest_it_carries(tmp_path):
    """The caption's licence to say "smaller", stated as a number rather
    than assumed by the client."""
    a, _ = roots(tmp_path)
    for i in range(us.MAX_SESSIONS + 10):
        write_transcript(a, cwd=f"/p/{i}", session_id=f"s{i:03d}",
                         turns=[dict(when=NOW - i, out=i + 1)])

    body = us.snapshot(roots=[a], now=NOW)

    assert body["largest_listed"] == us.MAX_SESSIONS
    ranked = sorted(body["sessions"],
                    key=lambda s: -s["total_tokens"]["total"])
    top = [s["session_id"] for s in ranked[:body["largest_listed"]]]
    biggest = sorted(range(us.MAX_SESSIONS + 10), reverse=True)[:us.MAX_SESSIONS]
    assert set(top) == {f"s{i:03d}" for i in biggest}


def test_a_short_machine_carries_every_conversation(tmp_path):
    a, _ = roots(tmp_path)
    for i in range(3):
        write_transcript(a, cwd=f"/p/{i}", session_id=f"s{i}",
                         turns=[dict(when=NOW - i, out=i + 1)])

    body = us.snapshot(roots=[a], now=NOW)

    assert body["session_count"] == 3
    assert len(body["sessions"]) == 3
    assert body["largest_listed"] == 3


def test_the_recent_conversations_are_still_carried(tmp_path):
    """The Sessions tab reads this payload too, to say what each LIVE
    conversation has spent. Ranking the truncation by spend alone would drop
    a conversation that started five minutes ago."""
    a, _ = roots(tmp_path)
    for i in range(us.MAX_SESSIONS + 10):
        write_transcript(a, cwd=f"/p/{i}", session_id=f"big{i:03d}",
                         turns=[dict(when=NOW - 100 * DAY - i, out=1_000_000)])
    write_transcript(a, cwd="/p/now", session_id="started-just-now",
                     turns=[dict(when=NOW - 60, out=3)])

    body = us.snapshot(roots=[a], now=NOW)

    assert "started-just-now" in [s["session_id"] for s in body["sessions"]]


def test_the_json_shape_of_an_empty_machine_says_so(tmp_path):
    body = us.snapshot(roots=roots(tmp_path), now=NOW)

    assert body["measured"] is False
    assert body["sessions"] == []
    assert body["daily"] == []
    assert body["totals"]["total"] == 0
    # `measured: false` is the flag the UI reads; the zero above must never be
    # rendered as a measurement on its own.


# ── robustness: these files are written live by other processes ────────────

def test_an_unreadable_transcript_does_not_take_the_report_down(tmp_path):
    a, _ = roots(tmp_path)
    write_transcript(a, cwd="/p/one", session_id="good",
                     turns=[dict(when=NOW - HOUR, out=10)])
    bad = a / "projects" / encode("/p/two") / "bad.jsonl"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"\xff\xfe not utf-8 and not json\n")

    report = us.report(roots=[a], now=NOW)

    assert one(report, "good").tokens.output == 10
    assert one(report, "bad").turns == 0


def test_a_directory_named_like_a_transcript_is_ignored(tmp_path):
    a, _ = roots(tmp_path)
    d = a / "projects" / encode("/p/one") / "weird.jsonl"
    d.mkdir(parents=True)

    assert us.report(roots=[a], now=NOW).sessions == []


@pytest.mark.parametrize("garbage", [None, "many", {"a": 1}, [], True])
def test_a_non_numeric_token_count_is_zero_not_a_crash(tmp_path, garbage):
    a, _ = roots(tmp_path)
    p = write_transcript(a, cwd="/p/one", session_id="s1",
                         turns=[dict(when=NOW - HOUR, out=5)])
    body = json.loads(p.read_text().strip().splitlines()[-1])
    body["message"]["usage"]["output_tokens"] = garbage
    with open(p, "a") as fh:
        fh.write(json.dumps(body) + "\n")

    assert one(us.report(roots=[a], now=NOW), "s1").tokens.output == 5


# ── "measured" is a claim about what was READ ───────────────────────────────
#
# `measured: false` is the flag the UI reads to decide whether the zeros
# beside it are a measurement or the arithmetic of an empty set. A file that
# was COUNTED but never opened made that flag lie: "Tokens, all time: 0"
# under "Read from 1 transcript".

def _unreadable(path: Path) -> None:
    """A transcript this process cannot open — the shape of a machine
    without Full Disk Access, which is the normal first-run state on macOS."""
    os.chmod(path, 0o000)


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                    reason="root can read anything; chmod 000 is a no-op on Windows")
def test_a_transcript_that_could_not_be_read_is_not_measured(tmp_path):
    a, _ = roots(tmp_path)
    p = write_transcript(a, cwd="/p/one", session_id="s1",
                         turns=[dict(when=NOW - HOUR, out=10)])
    _unreadable(p)
    try:
        report = us.report(roots=[a], now=NOW)
    finally:
        os.chmod(p, 0o644)

    assert report.totals.total == 0
    assert report.measured is False, (
        "the page would say 'Read from 1 transcript' over a zero it never read")
    assert report.files == 0


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                    reason="root can read anything; chmod 000 is a no-op on Windows")
def test_one_unreadable_file_does_not_unmeasure_the_readable_ones(tmp_path):
    a, _ = roots(tmp_path)
    good = write_transcript(a, cwd="/p/one", session_id="good",
                            turns=[dict(when=NOW - HOUR, out=10)])
    bad = write_transcript(a, cwd="/p/two", session_id="bad",
                           turns=[dict(when=NOW - HOUR, out=99)])
    _unreadable(bad)
    try:
        report = us.report(roots=[a], now=NOW)
    finally:
        os.chmod(bad, 0o644)

    assert report.measured is True
    assert report.files == 1
    assert report.totals.output == 10
    assert good.exists()


# ── a bad line never costs the turns around it ─────────────────────────────
#
# `scan_file` advanced its cursor BEFORE parsing and stored that cursor in
# the shared cache. An exception in the parse loop therefore left the cache
# pointing past bytes nobody had read: cold, the whole Usage page 503'd for
# ever; warm, it 503'd once and those turns were gone.
#
# The reachable exception, measured: `_epoch` catches only
# `fromisoformat`'s ValueError, and `day_key` on "0001-01-01T00:00:00.000Z"
# raises "year 0 is out of range" out of `datetime.fromtimestamp`.

ABSURD_STAMP = "0001-01-01T00:00:00.000Z"


def _turn_stamped(path: Path, session_id: str, cwd: str, stamp: str,
                  out: int) -> None:
    """Append one assistant turn carrying a timestamp of our choosing."""
    from tests.fixtures.transcripts import assistant_line
    line = json.loads(assistant_line(session_id=session_id, cwd=cwd,
                                     when=NOW, out=out))
    line["timestamp"] = stamp
    with open(path, "a") as fh:
        fh.write(json.dumps(line) + "\n")


def test_the_year_zero_timestamp_is_survivable_at_all(tmp_path):
    """The reviewer's reproduction, stated as the outcome: a cold scan of a
    transcript carrying this stamp must produce a report, not a 503."""
    a, _ = roots(tmp_path)
    p = write_transcript(a, cwd="/p/one", session_id="s1",
                         turns=[dict(when=NOW - HOUR, out=10)])
    _turn_stamped(p, "s1", "/p/one", ABSURD_STAMP, 7)

    report = us.report(roots=[a], now=NOW)

    assert one(report, "s1").tokens.output == 17


def test_a_bad_line_does_not_eat_the_turns_after_it(tmp_path):
    """The line itself may lose its DAY. It may not lose anybody's tokens,
    and it may not lose the turns that follow it in the same read."""
    a, _ = roots(tmp_path)
    p = write_transcript(a, cwd="/p/one", session_id="s1", turns=[])
    _turn_stamped(p, "s1", "/p/one", ABSURD_STAMP, 7)
    append_turns(p, session_id="s1", cwd="/p/one",
                 turns=[dict(when=NOW - HOUR, out=10)])

    s = one(us.report(roots=[a], now=NOW), "s1")

    assert s.turns == 2
    assert s.tokens.output == 17


def test_a_warm_cache_never_resumes_past_bytes_it_did_not_parse(tmp_path):
    """The permanent loss, reproduced directly.

    A cursor advanced before the parse and stored in the cache is a cursor
    that survives the exception; the next scan starts after the unparsed
    bytes and those turns are gone for the life of the process.
    """
    a, _ = roots(tmp_path)
    cache = us.Cache()
    p = write_transcript(a, cwd="/p/one", session_id="s1",
                         turns=[dict(when=NOW - HOUR, out=10)])
    us.report(roots=[a], now=NOW, cache=cache)

    _turn_stamped(p, "s1", "/p/one", ABSURD_STAMP, 7)
    append_turns(p, session_id="s1", cwd="/p/one",
                 turns=[dict(when=NOW - HOUR, out=5)])
    try:
        us.report(roots=[a], now=NOW, cache=cache)
    except Exception as e:                          # the 503 the page shows
        pytest.fail(f"the scan raised instead of skipping the line: {e!r}")

    s = one(us.report(roots=[a], now=NOW, cache=cache), "s1")
    assert s.tokens.output == 22, "turns were consumed and never counted"


def test_a_transient_stat_failure_does_not_zero_a_session(tmp_path,
                                                          monkeypatch):
    """One ENOENT between the listing and the stat used to drop that
    conversation to zero for the whole cache TTL — a real number replaced by
    a fake one, which is the failure this file exists to catch."""
    a, _ = roots(tmp_path)
    p = write_transcript(a, cwd="/p/one", session_id="s1",
                         turns=[dict(when=NOW - HOUR, out=10)])
    cache = us.Cache()
    assert one(us.report(roots=[a], now=NOW, cache=cache),
               "s1").tokens.output == 10

    real_stat = Path.stat

    def flaky(self, *args, **kwargs):
        if self == p:
            raise FileNotFoundError(2, "No such file or directory", str(p))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky)
    totals, read, was_read = us.scan_file(p, cache)

    assert read == 0
    assert was_read is False
    assert totals.tokens.output == 10, (
        "a transient stat failure reported the session as having spent zero")
