"""Which conversation in a project is the MAIN one, and what is in the background.

There was no such notion before. The rule implemented here, stated plainly:

    Primary is decided PER PROJECT. A conversation is ELIGIBLE if it is
    alive, has been prompted at least once, and was started interactively
    (`entrypoint: cli` -> origin "terminal"). Among the eligible ones, the
    most recently active wins — `since`, which is the roster's
    `statusUpdatedAt`. If the runner-up's activity is within
    PRIMARY_MARGIN_SEC of the leader's, NEITHER is primary: they are equally
    live and saying otherwise would be a guess dressed as a fact.

The margin is the whole point. A wrong guess presented confidently is worse
than an honest "these two look equally live", and every serious failure in
this project so far has looked like success.
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import session_watch as sw
from tests.fixtures.roster import write_roster, write_transcript

MS = 1000
NOW_MS = 1788404571964
NOW = NOW_MS / 1000.0


# A roster file is named for its pid, so several conversations need several
# pids — and every one of them has to look alive. `pid_alive` is exercised
# for real against this process and a dead pid in test_session_watch.py; here
# it is stubbed so the rule under test can be given the roster it needs.
LIVE_PID = 100_000
DEAD_PID = 999_999


@pytest.fixture(autouse=True)
def _pids_are_alive_below_the_dead_one(monkeypatch):
    monkeypatch.setattr(sw, "pid_alive", lambda pid: 0 < int(pid) < DEAD_PID)


_next_pid = iter(range(LIVE_PID, LIVE_PID + 500))


def live(root, *, session_id, cwd, name, active_ms, entrypoint="cli",
         status="idle", prompted=True):
    write_roster(root, pid=next(_next_pid), session_id=session_id, cwd=cwd,
                 name=name, entrypoint=entrypoint, status=status,
                 started_at=active_ms - 3600 * MS, status_updated_at=active_ms)
    if prompted:
        write_transcript(root, cwd=cwd, session_id=session_id,
                         last_prompt="carry on")


def primaries(snap):
    return {s.session_id for s in snap.sessions if s.primary}


def by_id(snap, sid):
    return snap.by_id(sid)


# ── the rule ────────────────────────────────────────────────────────────────

def test_the_only_interactive_conversation_in_a_project_is_the_main_one(tmp_path):
    root = tmp_path / ".claude"
    live(root, session_id="a", cwd="/p/one", name="one", active_ms=NOW_MS)

    snap = sw.build_snapshot(roots=[root], now=NOW)

    assert primaries(snap) == {"a"}
    assert by_id(snap, "a").primary_reason == "the only live conversation here"


def test_the_most_recently_active_conversation_is_the_main_one(tmp_path):
    root = tmp_path / ".claude"
    live(root, session_id="fresh-work", cwd="/p/one", name="one-a",
         active_ms=NOW_MS)
    live(root, session_id="an-hour-ago", cwd="/p/one", name="one-b",
         active_ms=NOW_MS - 3600 * MS)

    snap = sw.build_snapshot(roots=[root], now=NOW)

    assert primaries(snap) == {"fresh-work"}
    assert by_id(snap, "fresh-work").primary_reason == "most recently active"
    assert by_id(snap, "an-hour-ago").primary_reason == "a background conversation"


def test_two_equally_live_conversations_leave_the_question_open(tmp_path):
    """Inside the margin the signal cannot separate them, so nothing claims
    to be the main one. This is the honest answer, not a fallback."""
    root = tmp_path / ".claude"
    live(root, session_id="a", cwd="/p/one", name="one-a", active_ms=NOW_MS)
    live(root, session_id="b", cwd="/p/one", name="one-b",
         active_ms=NOW_MS - 30 * MS)

    snap = sw.build_snapshot(roots=[root], now=NOW)

    assert primaries(snap) == set()
    for sid in ("a", "b"):
        assert by_id(snap, sid).primary_reason == "equally live as another here"


def test_the_margin_is_the_line_and_just_past_it_separates_them(tmp_path):
    root = tmp_path / ".claude"
    gap_ms = int(sw.PRIMARY_MARGIN_SEC * MS) + MS
    live(root, session_id="a", cwd="/p/one", name="one-a", active_ms=NOW_MS)
    live(root, session_id="b", cwd="/p/one", name="one-b",
         active_ms=NOW_MS - gap_ms)

    assert primaries(sw.build_snapshot(roots=[root], now=NOW)) == {"a"}


def test_a_background_conversation_is_never_the_main_one(tmp_path):
    """`entrypoint: sdk-cli` means nobody is typing into it. Even when it is
    by far the most recently active thing in the project, the MAIN session is
    the one a person is sitting at."""
    root = tmp_path / ".claude"
    live(root, session_id="bg", cwd="/p/one", name="one-bg",
         active_ms=NOW_MS, entrypoint="sdk-cli")
    live(root, session_id="me", cwd="/p/one", name="one-me",
         active_ms=NOW_MS - 4 * 3600 * MS)

    snap = sw.build_snapshot(roots=[root], now=NOW)

    assert primaries(snap) == {"me"}
    assert by_id(snap, "bg").primary is False
    assert by_id(snap, "bg").primary_reason == "a background conversation"


def test_a_project_of_nothing_but_background_work_has_no_main_session(tmp_path):
    root = tmp_path / ".claude"
    live(root, session_id="bg1", cwd="/p/one", name="a", active_ms=NOW_MS,
         entrypoint="sdk-cli")
    live(root, session_id="bg2", cwd="/p/one", name="b",
         active_ms=NOW_MS - 9999 * MS, entrypoint="sdk-cli")

    snap = sw.build_snapshot(roots=[root], now=NOW)

    assert primaries(snap) == set()


def test_a_conversation_nobody_has_prompted_is_not_the_main_one(tmp_path):
    """`fresh` means no transcript at all: it has never been used. It is not
    what the user means by "the main session" even when it is the newest."""
    root = tmp_path / ".claude"
    live(root, session_id="never-used", cwd="/p/one", name="a",
         active_ms=NOW_MS, prompted=False)
    live(root, session_id="real", cwd="/p/one", name="b",
         active_ms=NOW_MS - 3 * 3600 * MS)

    snap = sw.build_snapshot(roots=[root], now=NOW)

    assert by_id(snap, "never-used").state == sw.FRESH
    assert primaries(snap) == {"real"}
    assert by_id(snap, "never-used").primary_reason == "never prompted"


def test_a_dead_conversation_is_not_the_main_one(tmp_path):
    root = tmp_path / ".claude"
    write_roster(root, pid=DEAD_PID, session_id="dead", cwd="/p/one", name="a",
                 status_updated_at=NOW_MS)
    write_transcript(root, cwd="/p/one", session_id="dead", last_prompt="x")
    live(root, session_id="alive", cwd="/p/one", name="b",
         active_ms=NOW_MS - 3 * 3600 * MS)

    snap = sw.build_snapshot(roots=[root], now=NOW)

    assert by_id(snap, "dead").state == sw.GONE
    assert primaries(snap) == {"alive"}
    assert by_id(snap, "dead").primary_reason == "finished"


def test_each_project_gets_its_own_main_session(tmp_path):
    """"The main one" is a question about a project, not about the machine —
    a person works in several at once."""
    root = tmp_path / ".claude"
    live(root, session_id="one-new", cwd="/p/one", name="a", active_ms=NOW_MS)
    live(root, session_id="one-old", cwd="/p/one", name="b",
         active_ms=NOW_MS - 3 * 3600 * MS)
    live(root, session_id="two-only", cwd="/p/two", name="c",
         active_ms=NOW_MS - 8 * 3600 * MS)

    snap = sw.build_snapshot(roots=[root], now=NOW)

    assert primaries(snap) == {"one-new", "two-only"}


def test_an_entry_with_no_status_stamp_falls_back_to_when_it_started(tmp_path):
    """A roster entry with no `statusUpdatedAt` at all was measured live (an
    sdk-cli one). `since` falls back to `startedAt`, so it still ranks — it
    just ranks by the only stamp it has, and an old start does not beat
    somebody active a moment ago."""
    root = tmp_path / ".claude"
    write_roster(root, pid=next(_next_pid), session_id="stampless", cwd="/p/one",
                 name="a", status=None, status_updated_at=None,
                 started_at=NOW_MS - 5 * 3600 * MS)
    write_transcript(root, cwd="/p/one", session_id="stampless", last_prompt="x")
    live(root, session_id="stamped", cwd="/p/one", name="b",
         active_ms=NOW_MS)

    snap = sw.build_snapshot(roots=[root], now=NOW)

    assert by_id(snap, "stampless").since is not None
    assert primaries(snap) == {"stamped"}


def test_a_conversation_with_no_stamp_at_all_never_outranks_a_stamped_one(tmp_path):
    """With neither stamp, `since` is None. That is an ABSENCE of evidence
    about recency and must sort last — sorting it first (or treating None as
    "now") would hand the crown to the one conversation we know least about.
    """
    root = tmp_path / ".claude"
    # Written by hand rather than through `write_roster`, whose signature
    # (shared with other tests) assumes a `startedAt` is always there.
    pid = next(_next_pid)
    d = root / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.json").write_text(json.dumps({
        "pid": pid, "sessionId": "unstamped", "cwd": "/p/one",
        "name": "a", "entrypoint": "cli"}), encoding="utf-8")
    write_transcript(root, cwd="/p/one", session_id="unstamped", last_prompt="x")
    live(root, session_id="stamped", cwd="/p/one", name="b",
         active_ms=NOW_MS - 9 * 3600 * MS)

    snap = sw.build_snapshot(roots=[root], now=NOW)

    assert by_id(snap, "unstamped").since is None
    assert primaries(snap) == {"stamped"}


# ── it reaches the API ──────────────────────────────────────────────────────

def test_the_json_shape_carries_the_verdict_and_the_reason(tmp_path):
    """A boolean alone is a claim. The reason is what makes it checkable."""
    root = tmp_path / ".claude"
    live(root, session_id="a", cwd="/p/one", name="one", active_ms=NOW_MS)

    snap = sw.build_snapshot(roots=[root], now=NOW)
    body = sw.session_to_dict(snap.by_id("a"))

    assert body["primary"] is True
    assert body["primary_reason"] == "the only live conversation here"
    assert body["agents_active"] == 0
    assert body["agents_seen"] == 0


def test_the_json_shape_carries_started_and_since_as_two_separate_fields(tmp_path):
    """`started` (when the conversation began) and `since` (when its CURRENT
    STATE began) are different quantities. Measured on the live roster the
    gap between them reached 102 HOURS, so a UI that showed one where it
    meant the other would be off by four days and look entirely plausible.

    /api/sessions carried only `since`, which meant the dashboard could not
    say how old a conversation was without lying about it. Both go over the
    wire, named for what they are.
    """
    root = tmp_path / ".claude"
    started_ms = NOW_MS - 102 * 3600 * MS
    touched_ms = NOW_MS - 5 * 60 * MS
    write_roster(root, pid=LIVE_PID + 400, session_id="old", cwd="/p/one",
                 name="one", started_at=started_ms,
                 status_updated_at=touched_ms)
    write_transcript(root, cwd="/p/one", session_id="old",
                     last_prompt="carry on")

    body = sw.session_to_dict(sw.build_snapshot(roots=[root], now=NOW).by_id("old"))

    assert body["started"] == pytest.approx(started_ms / 1000.0)
    assert body["since"] == pytest.approx(touched_ms / 1000.0)
    assert body["started"] != body["since"]


def test_a_session_with_no_start_stamp_reports_started_as_null_not_zero(tmp_path):
    """An absent stamp is an absence of evidence. `0` would render as
    1 January 1970 and look like a measurement."""
    root = tmp_path / ".claude"
    write_roster(root, pid=LIVE_PID + 401, session_id="nostamp", cwd="/p/two",
                 name="two", extra={"startedAt": None}, status_updated_at=None)
    write_transcript(root, cwd="/p/two", session_id="nostamp",
                     last_prompt="carry on")

    body = sw.session_to_dict(sw.build_snapshot(roots=[root], now=NOW).by_id("nostamp"))

    assert body["started"] is None
    assert body["since"] is None


# ── subagents ───────────────────────────────────────────────────────────────

def _agent_file(root, cwd, session_id, agent_id, mtime):
    from tests.fixtures.transcripts import encode
    d = root / "projects" / encode(cwd) / session_id / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"agent-{agent_id}.jsonl"
    p.write_text('{"type":"assistant","isSidechain":true}\n', encoding="utf-8")
    os.utime(p, (mtime, mtime))
    return p


def test_a_session_reports_how_many_subagents_are_working_under_it(tmp_path):
    """The only signal available is the file: a subagent transcript written
    seconds ago is working. Reported against a STATED window, and never
    called a live process, because it is not one."""
    root = tmp_path / ".claude"
    live(root, session_id="a", cwd="/p/one", name="one", active_ms=NOW_MS)
    _agent_file(root, "/p/one", "a", "now", NOW - 5)
    _agent_file(root, "/p/one", "a", "also-now", NOW - 20)
    _agent_file(root, "/p/one", "a", "long-done", NOW - 4000)

    snap = sw.build_snapshot(roots=[root], now=NOW)

    s = snap.by_id("a")
    assert s.agents_seen == 3
    assert s.agents_active == 2


def test_the_sidecars_beside_the_transcripts_do_not_eat_the_cap(tmp_path):
    """Found live: one `subagents/` folder held 418 entries — 209 `.jsonl`
    transcripts and 209 `.json` sidecars. Capping the directory listing
    BEFORE filtering to transcripts reported 150 agents where there were
    209, and the short number looked exactly like a real one.
    """
    root = tmp_path / ".claude"
    live(root, session_id="a", cwd="/p/one", name="one", active_ms=NOW_MS)
    # Deliberately WELL UNDER the cap in transcripts, and over it in total
    # entries. Sizing this at the cap would make a broken listing return the
    # right number by accident, which is what the first version of this test
    # did — it passed with the bug reinstated.
    agents = 100
    for i in range(agents):
        _agent_file(root, "/p/one", "a", f"n{i:04d}", NOW - 4000)
        # The sidecar the CLI writes beside every one of them.
        (root / "projects" / "-p-one" / "a" / "subagents"
         / f"agent-n{i:04d}.meta.json").write_text('{"agentType":"general-purpose"}', encoding="utf-8")
    entries = len(list((root / "projects" / "-p-one" / "a" / "subagents").iterdir()))
    assert entries == 2 * agents

    s = sw.build_snapshot(roots=[root], now=NOW).by_id("a")

    assert s.agents_seen == agents, (
        "the count must be of transcripts, not of directory entries")
    assert s.agents_capped is False


def test_a_capped_agent_count_says_it_is_a_floor(tmp_path):
    """A count that hit the cap is a lower bound. Reporting it as a total
    would be a number we do not have."""
    root = tmp_path / ".claude"
    live(root, session_id="a", cwd="/p/one", name="one", active_ms=NOW_MS)
    for i in range(sw.MAX_AGENT_FILES + 5):
        _agent_file(root, "/p/one", "a", f"n{i:04d}", NOW - 4000)

    s = sw.build_snapshot(roots=[root], now=NOW).by_id("a")

    assert s.agents_seen == sw.MAX_AGENT_FILES
    assert s.agents_capped is True
    assert sw.session_to_dict(s)["agents_capped"] is True


def test_an_uncapped_agent_count_is_not_a_floor(tmp_path):
    root = tmp_path / ".claude"
    live(root, session_id="a", cwd="/p/one", name="one", active_ms=NOW_MS)
    _agent_file(root, "/p/one", "a", "x", NOW - 4000)

    s = sw.build_snapshot(roots=[root], now=NOW).by_id("a")

    assert s.agents_seen == 1 and s.agents_capped is False


def test_a_session_that_dispatched_nothing_reports_no_agents(tmp_path):
    root = tmp_path / ".claude"
    live(root, session_id="a", cwd="/p/one", name="one", active_ms=NOW_MS)

    s = sw.build_snapshot(roots=[root], now=NOW).by_id("a")

    assert s.agents_seen == 0 and s.agents_active == 0


def test_counting_subagents_never_takes_a_poll_down(tmp_path):
    """These folders belong to another process and can vanish mid-read."""
    root = tmp_path / ".claude"
    live(root, session_id="a", cwd="/p/one", name="one", active_ms=NOW_MS)
    bad = root / "projects" / "-p-one" / "a" / "subagents"
    bad.mkdir(parents=True)
    (bad / "agent-x.jsonl").mkdir()          # a directory where a file goes

    s = sw.build_snapshot(roots=[root], now=NOW).by_id("a")

    assert s.agents_seen == 0


def test_a_conversation_that_died_gives_up_the_crown_on_the_next_poll(tmp_path):
    """The watcher carries a dead conversation forward for ten minutes so its
    completion can still be announced. That cached record was marked primary
    in an EARLIER poll, against whoever was live THEN. Primary is a
    comparison, so it has to be recomputed over the final set — or the
    dashboard goes on calling a finished session the main one."""
    root = tmp_path / ".claude"
    live(root, session_id="a", cwd="/p/one", name="one", active_ms=NOW_MS)
    watcher = sw.SessionWatcher(roots=[root])

    first = watcher.poll_once(now=NOW)
    assert primaries(first) == {"a"}

    for f in (root / "sessions").glob("*.json"):
        f.unlink()
    second = watcher.poll_once(now=NOW + 1)

    carried = second.by_id("a")
    assert carried is not None and carried.state == sw.GONE, "not carried forward"
    assert carried.primary is False
    assert carried.primary_reason == "finished"


def test_the_watcher_still_polls_with_the_primary_rule_in_place(tmp_path):
    """The rule runs inside build_snapshot, which the 1 Hz watcher drives.
    A deadline on this: a poll that hangs is the failure mode that hides."""
    root = tmp_path / ".claude"
    live(root, session_id="a", cwd="/p/one", name="one", active_ms=NOW_MS)
    watcher = sw.SessionWatcher(roots=[root])

    started = time.monotonic()
    snap = watcher.poll_once(now=NOW)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"a poll took {elapsed:.1f}s"
    assert primaries(snap) == {"a"}
