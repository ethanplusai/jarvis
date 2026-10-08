import json
import os
import pytest
from pathlib import Path

import session_watch as sw
from tests.fixtures.roster import write_roster, write_transcript


def test_read_roster_unions_both_roots_and_parses_every_observed_field(tmp_path):
    a, b = tmp_path / ".claude", tmp_path / ".claude-orcha"
    write_roster(a, pid=os.getpid(), session_id="s-a", cwd="/p/one", name="one-4b")
    write_roster(b, pid=os.getpid(), session_id="s-b", cwd="/p/two", name="two-9c",
                 status="waiting", waiting_for="permission prompt", socket=False)

    entries = sw.read_roster([a, b])

    assert {e.session_id for e in entries} == {"s-a", "s-b"}
    one = next(e for e in entries if e.session_id == "s-a")
    assert one.name == "one-4b" and one.cwd == "/p/one"
    assert one.socket_path == f"/tmp/cc-socks/{os.getpid()}.sock"
    assert one.status == "idle" and one.waiting_for is None
    # startedAt is epoch MILLISECONDS on disk; we expose seconds.
    assert 1788113511 < one.started_at < 1799999999

    two = next(e for e in entries if e.session_id == "s-b")
    assert two.status == "waiting" and two.waiting_for == "permission prompt"
    assert two.socket_path is None


def test_a_roster_entry_missing_optional_fields_still_parses(tmp_path):
    """Measured: one live entry (an sdk-cli one) had no `status` and no
    `statusUpdatedAt` at all. It must not be dropped or raise."""
    root = tmp_path / ".claude"
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n",
                 status=None, status_updated_at=None, entrypoint="sdk-cli")

    entry, = sw.read_roster([root])

    assert entry.status is None and entry.status_updated_at is None
    assert entry.entrypoint == "sdk-cli"


def test_unreadable_and_half_written_roster_files_are_skipped_not_raised(tmp_path):
    """The CLI writes these files live; a tick must survive reading one mid-write."""
    root = tmp_path / ".claude"
    write_roster(root, pid=os.getpid(), session_id="good", cwd="/p", name="n")
    (root / "sessions" / "999999.json").write_text('{"pid": 999999, "sess')  # truncated
    (root / "sessions" / "888888.json").write_text("")                       # empty
    (root / "sessions" / "777777.json").write_text('{"no": "pid"}')          # missing keys

    entries = sw.read_roster([root])

    assert [e.session_id for e in entries] == ["good"]


def test_missing_root_is_not_an_error(tmp_path):
    assert sw.read_roster([tmp_path / "nope", tmp_path / "also-nope"]) == []


def test_pid_alive_distinguishes_this_process_from_a_dead_one(tmp_path):
    assert sw.pid_alive(os.getpid()) is True
    assert sw.pid_alive(999999) is False
    assert sw.pid_alive(None) is False
    assert sw.pid_alive(0) is False


def test_encode_cwd_replaces_every_non_alphanumeric_including_dots(tmp_path):
    """The `/`→`-` shortcut misses dots and would never find a worktree
    transcript. Verified against the live path."""
    assert sw.encode_cwd("/Users/e/Projects/jarvis/.claude/worktrees/runs-dashboard") == \
        "-Users-e-Projects-jarvis--claude-worktrees-runs-dashboard"
    assert sw.encode_cwd("/a/b.c") == "-a-b-c"


def test_project_name_resolves_a_claude_code_worktree_to_its_repo_name():
    """Claude Code creates worktrees under `<repo>/.claude/worktrees/<branch>`.
    Verified live: a plain `Path(cwd).name` on such a path yields the
    worktree/branch name ("runs-dashboard"), not the repo the user actually
    means ("jarvis"), so `resolve("jarvis")` found nothing."""
    cwd = "/Users/e/Projects/jarvis/.claude/worktrees/runs-dashboard"
    assert sw.project_name(cwd) == "jarvis"


def test_project_name_is_unaffected_for_a_normal_non_worktree_path():
    assert sw.project_name("/Users/e/Projects/chitauri") == "chitauri"
    assert sw.project_name("/Users/e/Desktop/webapp-fresh") == \
        "webapp-fresh"


def test_config_roots_includes_both_defaults_and_the_env_extras(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_CLAUDE_CONFIG_DIRS", f"{tmp_path}/x{os.pathsep}{tmp_path}/y")
    roots = sw.config_roots()
    names = [r.name for r in roots]
    assert ".claude" in names and ".claude-orcha" in names
    assert Path(f"{tmp_path}/x") in roots and Path(f"{tmp_path}/y") in roots


def test_recap_prefers_the_cli_s_own_title_and_last_prompt_lines(tmp_path):
    """`ai-title` and `last-prompt` are written by the CLI on every turn, so
    the recap needs almost no parsing. Measured on the live transcripts."""
    root = tmp_path / ".claude"
    write_transcript(root, cwd="/p/hammer", session_id="s1",
                     title="Review and improve the hammer worker",
                     last_prompt="I like the light mode better but both have issues",
                     assistant_texts=["Done and pushed to the private repo."],
                     tools=["Bash", "Edit", "Bash"])

    r = sw.read_recap(root, "/p/hammer", "s1")

    assert r.exists is True
    assert r.title == "Review and improve the hammer worker"
    assert r.last_prompt.startswith("I like the light mode better")
    assert r.last_text == "Done and pushed to the private repo."
    assert r.recent_tools == ["Bash", "Edit", "Bash"]


def test_recap_survives_a_file_far_larger_than_the_tail(tmp_path):
    """Live transcripts reach 95 MB. We read the last 64 KB and nothing more."""
    root = tmp_path / ".claude"
    p = write_transcript(root, cwd="/p/big", session_id="s2", padding_kb=400,
                         title="Big one", last_prompt="carry on",
                         assistant_texts=["Nearly there."], tools=["Bash"])
    assert p.stat().st_size > 300 * 1024

    r = sw.read_recap(root, "/p/big", "s2")

    assert (r.title, r.last_prompt, r.last_text) == ("Big one", "carry on", "Nearly there.")


def test_a_partial_first_line_in_the_tail_is_discarded_not_crashed(tmp_path):
    """Seeking into the middle of a 95 MB file lands mid-line every time."""
    root = tmp_path / ".claude"
    p = write_transcript(root, cwd="/p/x", session_id="s3", padding_kb=200,
                         title="T", last_prompt="P", assistant_texts=["A"])
    objs = sw.tail_objects(p, nbytes=4096)
    assert objs, "the tail must still yield whole lines"
    assert all(isinstance(o, dict) for o in objs)


def test_a_partial_first_line_that_would_itself_parse_is_still_discarded(tmp_path):
    """The discard only matters when the bytes right after the seek point
    happen to form a complete, valid JSON object on their own — a truncated
    line is normally invalid JSON and gets skipped by the parse `except`
    regardless of the discard, which is why the no-crash test above can't
    tell a missing discard from a present one.

    Build the one case where it matters: the seek point lands EXACTLY on the
    '{' of a well-formed object embedded after garbage padding on the file's
    first physical line. With the discard, that whole padded line — garbage
    prefix and embedded object alike — is thrown away and the tail starts
    clean at the next line. Without it, the embedded object is read as a
    genuine leading line.

    Because `ai-title` assignment is last-write-wins, a *second*, later
    `ai-title` line would overwrite the embedded one either way and hide the
    bug — so here the embedded object is the only `ai-title` in the file, and
    the real content that must survive the tail is carried by `last-prompt`
    instead. Verified: with the discard, tail_objects never surfaces the
    embedded object, so `title` stays unset and `summary()` falls back to the
    real last-prompt; deleting the discard (see the fix report) makes `title`
    come back "GHOST", proving the discard is what excluded it."""
    root = tmp_path / ".claude"
    d = root / "projects" / sw.encode_cwd("/p/ghost")
    d.mkdir(parents=True)
    p = d / "s3b.jsonl"

    ghost_obj = '{"type": "ai-title", "aiTitle": "GHOST"}'
    first_line = ("X" * 5000) + ghost_obj  # NOT valid JSON as a whole line
    lines = [
        first_line,
        json.dumps({"type": "last-prompt", "lastPrompt": "REAL"}),
        json.dumps({"type": "assistant", "isSidechain": False,
                    "message": {"role": "assistant",
                                "content": [{"type": "text", "text": "A"}]}}),
    ]
    content = "\n".join(lines) + "\n"
    p.write_text(content)

    brace_index = content.index(ghost_obj)
    nbytes = len(content.encode("utf-8")) - brace_index  # seek lands exactly on '{'

    r = sw.recap_from(sw.tail_objects(p, nbytes=nbytes))

    assert r.title is None, "the embedded 'GHOST' title must not surface"
    assert r.summary() == "REAL"


def test_subagent_lines_are_excluded_from_the_recap(tmp_path):
    """`write_transcript`'s `sidechain_text` is always written BEFORE the
    `assistant_texts` lines, so a test built purely on that fixture only
    proves ordering (the later real line always wins), not filtering — it
    passes even with the `isSidechain` guard deleted. Build the file by hand
    with the subagent text LAST, so only correct filtering — not
    last-write-wins — can produce the right answer."""
    root = tmp_path / ".claude"
    d = root / "projects" / sw.encode_cwd("/p/s")
    d.mkdir(parents=True)
    (d / "s4.jsonl").write_text("\n".join([
        json.dumps({"type": "assistant", "isSidechain": False,
                    "message": {"role": "assistant",
                                "content": [{"type": "text",
                                             "text": "The real answer."}]}}),
        json.dumps({"type": "assistant", "isSidechain": True,
                    "message": {"role": "assistant",
                                "content": [{"type": "text",
                                             "text": "I am a subagent and should not be quoted."}]}}),
        json.dumps({"type": "ai-title", "aiTitle": "T"}),
        json.dumps({"type": "last-prompt", "lastPrompt": "P"}),
        "",
    ]) + "\n")

    r = sw.read_recap(root, "/p/s", "s4")

    assert r.last_text == "The real answer."


def test_a_malformed_message_field_is_skipped_not_raised(tmp_path):
    """`message` is normally a dict (or absent/None, already handled). A line
    with a truthy non-dict `message` — a string, or a non-empty list — must
    be skipped like any other malformed line, not raise `AttributeError`."""
    objs = [
        {"type": "assistant", "isSidechain": False, "message": "a string"},
        {"type": "assistant", "isSidechain": False, "message": ["not", "a", "dict"]},
        {"type": "assistant", "isSidechain": False,
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "Still works."}]}},
    ]

    r = sw.recap_from(objs)

    assert r.last_text == "Still works."


def test_a_session_with_no_transcript_is_reported_as_not_existing(tmp_path):
    """This is the `fresh` signal: a session nobody has prompted writes no
    .jsonl at all. Three of seventeen live sessions were in this state."""
    r = sw.read_recap(tmp_path / ".claude", "/p/never", "nope")

    assert r.exists is False
    assert r.title is None and r.last_prompt is None and r.last_text is None
    assert r.recent_tools == []


def test_unknown_line_types_and_broken_lines_are_ignored(tmp_path):
    """19 line types were observed and only 5 are used; new ones must not break."""
    root = tmp_path / ".claude"
    d = root / "projects" / sw.encode_cwd("/p/odd")
    d.mkdir(parents=True)
    (d / "s5.jsonl").write_text("\n".join([
        '{"type": "brand-new-type-from-a-future-cli", "payload": {"a": 1}}',
        "not json at all",
        '{"type": "ai-title", "aiTitle": "Still found it"}',
        '{"type": "last-prompt", "lastPrompt": "and this"}',
        "",
    ]) + "\n")

    r = sw.read_recap(root, "/p/odd", "s5")

    assert r.title == "Still found it" and r.last_prompt == "and this"


def test_recap_falls_back_to_the_last_user_message_when_there_is_no_title(tmp_path):
    """Roughly half the live sessions had no `ai-title` line."""
    root = tmp_path / ".claude"
    write_transcript(root, cwd="/p/n", session_id="s6", title=None,
                     last_prompt="ok yeah lets work on the brand guidelines",
                     assistant_texts=["Enough context."])

    r = sw.read_recap(root, "/p/n", "s6")

    assert r.title is None
    assert r.summary() == "ok yeah lets work on the brand guidelines"


def test_recent_tools_are_capped_and_most_recent(tmp_path):
    root = tmp_path / ".claude"
    write_transcript(root, cwd="/p/t", session_id="s7", title="T", last_prompt="P",
                     tools=["A", "B", "C", "D", "E", "F", "G"])

    assert sw.read_recap(root, "/p/t", "s7").recent_tools == ["C", "D", "E", "F", "G"]


# --- conversations -----------------------------------------------------------

# Two pids that are genuinely alive during the test run, so `pid_alive` is
# exercised for real rather than stubbed. The roster file is named after the
# pid, so distinct pids are also what makes distinct files possible.
LIVE_A = os.getpid()
LIVE_B = os.getppid()
DEAD = 999999


def test_several_processes_on_one_session_id_collapse_to_one_conversation(tmp_path):
    """Measured live: pids 32929/33585/33708 shared one hammer conversation and
    one 22 MB transcript. Counting processes is the bug this fixes."""
    root = tmp_path / ".claude"
    write_roster(root, pid=LIVE_A, session_id="sh", cwd="/p/hammer",
                 name="hammer-4b", socket=False)
    write_roster(root, pid=DEAD, session_id="sh", cwd="/p/hammer",
                 name="hammer-18")          # dead, but it does have a socket
    write_transcript(root, cwd="/p/hammer", session_id="sh", title="Hammer work",
                     last_prompt="carry on")

    snap = sw.build_snapshot(roots=[root])

    assert len(snap.sessions) == 1, "two processes, one conversation"
    s = snap.sessions[0]
    assert s.session_id == "sh"
    assert sorted(s.pids) == sorted([LIVE_A, DEAD])
    assert s.primary_pid == LIVE_A, "a live process outranks a dead one with a socket"


def test_one_process_registered_under_two_roots_is_counted_once(tmp_path):
    """The dedupe case `read_roster` exists for: Orcha sets CLAUDE_CONFIG_DIR,
    so a single process can appear in both roots. Same pid AND same session id
    is one process; same session id with a DIFFERENT pid is not (that is the
    several-processes-per-conversation case above, which must stay separate)."""
    a, b = tmp_path / ".claude", tmp_path / ".claude-orcha"
    write_roster(a, pid=LIVE_A, session_id="s", cwd="/p", name="n")
    write_roster(b, pid=LIVE_A, session_id="s", cwd="/p", name="n")
    write_transcript(a, cwd="/p", session_id="s", title="T", last_prompt="P")

    entries = sw.read_roster([a, b])

    assert len(entries) == 1, "one process, listed twice, is still one process"
    assert sw.build_snapshot(roots=[a, b]).sessions[0].pids == [LIVE_A]


def test_the_primary_process_prefers_alive_then_socket_then_recency(tmp_path):
    """Two live processes on one conversation: the one holding a socket wins,
    because it is the only one that can be steered."""
    root = tmp_path / ".claude"
    write_roster(root, pid=LIVE_A, session_id="s", cwd="/p", name="a",
                 socket=False, status_updated_at=1000)
    write_roster(root, pid=LIVE_B, session_id="s", cwd="/p", name="b",
                 socket=True, status_updated_at=2000)
    write_transcript(root, cwd="/p", session_id="s", title="T", last_prompt="P")

    # `write_roster(socket=True)` only writes the `messagingSocketPath` field;
    # `RosterEntry.steerable` also requires the socket file to really exist
    # on disk (that's the point of the property — 4/17 live entries on the
    # real machine had the field but no live socket). Stand up a real file at
    # that exact path for the duration of this test so the "socket wins"
    # precedence is actually exercised, and leave it exactly as found.
    sock_path = Path(f"/tmp/cc-socks/{LIVE_B}.sock")
    made_dir = not sock_path.parent.exists()
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    made_file = not sock_path.exists()
    if made_file:
        sock_path.touch()
    try:
        s, = sw.build_snapshot(roots=[root]).sessions

        assert s.primary_pid == LIVE_B
        assert s.steerable is True, "a socket on any live process makes it steerable"
    finally:
        if made_file:
            sock_path.unlink(missing_ok=True)
        if made_dir:
            sock_path.parent.rmdir()


# --- state derivation --------------------------------------------------------

@pytest.mark.parametrize("status,waiting_for,expected", [
    ("busy", None, sw.WORKING),
    ("idle", None, sw.IDLE),
    ("shell", None, sw.SHELL),
    ("waiting", "permission prompt", sw.NEEDS_YOU),
    ("waiting", None, sw.NEEDS_YOU),
    ("idle", "dialog open", sw.NEEDS_YOU),
    (None, None, sw.UNKNOWN),
])
def test_state_is_derived_from_status_and_waiting_for(tmp_path, status, waiting_for, expected):
    root = tmp_path / ".claude"
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n",
                 status=status, waiting_for=waiting_for)
    write_transcript(root, cwd="/p", session_id="s", title="T", last_prompt="P")

    assert sw.build_snapshot(roots=[root]).sessions[0].state == expected


def test_a_session_with_no_transcript_is_fresh_whatever_its_status(tmp_path):
    """Live: chitauri-67 was `waiting`/`dialog open` with no transcript — a
    session sitting at a startup dialog nobody has spoken to. Never announced."""
    root = tmp_path / ".claude"
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n",
                 status="waiting", waiting_for="dialog open")

    s, = sw.build_snapshot(roots=[root]).sessions
    assert s.state == sw.FRESH
    assert s.announceable is False


def test_a_conversation_whose_processes_are_all_dead_is_gone(tmp_path):
    root = tmp_path / ".claude"
    write_roster(root, pid=999999, session_id="s", cwd="/p", name="n", status="busy")
    write_transcript(root, cwd="/p", session_id="s", title="T", last_prompt="P")

    s, = sw.build_snapshot(roots=[root]).sessions
    assert s.state == sw.GONE and s.steerable is False


def test_waiting_on_any_attached_process_blocks_the_whole_conversation(tmp_path):
    """A permission prompt on one attached process stops the conversation."""
    root = tmp_path / ".claude"
    write_roster(root, pid=LIVE_A, session_id="s", cwd="/p", name="a", status="idle")
    write_roster(root, pid=LIVE_B, session_id="s", cwd="/p", name="a",
                 status="waiting", waiting_for="permission prompt")
    write_transcript(root, cwd="/p", session_id="s", title="T", last_prompt="P")

    s, = sw.build_snapshot(roots=[root]).sessions
    assert s.state == sw.NEEDS_YOU
    assert s.needs == "permission prompt"
    assert s.needs_a_human_hand is True, "the socket cannot dismiss a permission prompt"


def test_a_question_in_the_last_message_makes_an_idle_session_need_you(tmp_path):
    root = tmp_path / ".claude"
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n", status="idle")
    write_transcript(root, cwd="/p", session_id="s", title="T", last_prompt="go",
                     assistant_texts=["Which database should I use, Postgres or SQLite?"])

    s, = sw.build_snapshot(roots=[root]).sessions
    assert s.state == sw.NEEDS_YOU
    assert s.needs_a_human_hand is False, "a question CAN be answered over the socket"


def test_an_asked_question_tool_use_makes_a_session_need_you(tmp_path):
    root = tmp_path / ".claude"
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n", status="idle")
    write_transcript(root, cwd="/p", session_id="s", title="T", last_prompt="go",
                     tools=["Bash", "AskUserQuestion"])

    assert sw.build_snapshot(roots=[root]).sessions[0].state == sw.NEEDS_YOU


# --- voice names -------------------------------------------------------------
#
# These tests care about naming, not liveness. Arbitrary pids like `getpid()+1`
# are usually dead (making every conversation `gone`) and occasionally alive on
# a busy machine — flaky in both directions — so liveness is pinned explicitly.

@pytest.fixture
def all_alive(monkeypatch):
    monkeypatch.setattr(sw, "pid_alive", lambda pid: True)


def test_a_lone_project_is_named_by_its_folder(tmp_path, all_alive):
    root = tmp_path / ".claude"
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/Users/e/Desktop/chitauri",
                 name="chitauri-67")
    write_transcript(root, cwd="/Users/e/Desktop/chitauri", session_id="s",
                     title="T", last_prompt="P")

    assert sw.build_snapshot(roots=[root]).sessions[0].voice_name == "chitauri"


def test_same_named_projects_in_different_folders_are_named_by_folder(tmp_path, all_alive):
    """Measured: chitauri exists on the Desktop and in Projects."""
    root = tmp_path / ".claude"
    me = os.getpid()
    for i, cwd in enumerate(("/Users/e/Desktop/chitauri", "/Users/e/Projects/chitauri")):
        write_roster(root, pid=me + i, session_id=f"s{i}", cwd=cwd, name=f"chitauri-{i}")
        write_transcript(root, cwd=cwd, session_id=f"s{i}", title="T", last_prompt="P")

    names = {s.voice_name for s in sw.build_snapshot(roots=[root]).sessions}
    assert names == {"chitauri in Desktop", "chitauri in Projects"}


def test_two_conversations_in_one_folder_are_named_by_age(tmp_path, all_alive):
    """Measured: hammer had two conversations in the SAME directory, so the
    folder cannot disambiguate them and the roster suffix is unsayable."""
    root = tmp_path / ".claude"
    me = os.getpid()
    write_roster(root, pid=me, session_id="old", cwd="/p/hammer", name="hammer-4b",
                 started_at=1_000_000_000_000)
    write_roster(root, pid=me + 1, session_id="new", cwd="/p/hammer", name="hammer-18",
                 started_at=1_788_000_000_000)
    for sid in ("old", "new"):
        write_transcript(root, cwd="/p/hammer", session_id=sid, title="T", last_prompt="P")

    by_id = {s.session_id: s.voice_name for s in sw.build_snapshot(roots=[root]).sessions}
    assert by_id == {"old": "the older hammer", "new": "the newer hammer"}


def test_started_and_since_are_different_fields_with_different_sources(tmp_path, all_alive):
    """`started` (when the session began) and `since` (when the CURRENT STATE
    began) must NOT collapse into one value. Measured live: the largest gap
    between `startedAt` and `statusUpdatedAt` was 102 hours (VoyageStudios,
    pid 37497) — a spoken elapsed time built from the wrong one would be off
    by days. Build a roster entry where the two are far apart and check each
    field tracks its own source, in seconds (the roster stores milliseconds)."""
    root = tmp_path / ".claude"
    started_at_ms = 1_000_000_000_000
    status_updated_at_ms = started_at_ms + 367_200_000  # +102 hours, matching the live gap
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p/gap", name="n",
                 started_at=started_at_ms, status_updated_at=status_updated_at_ms)
    write_transcript(root, cwd="/p/gap", session_id="s", title="T", last_prompt="P")

    s, = sw.build_snapshot(roots=[root]).sessions

    assert s.started == started_at_ms / 1000.0
    assert s.since == status_updated_at_ms / 1000.0
    assert s.started != s.since
    assert (s.since - s.started) == 367_200.0  # 102 hours, in seconds


def test_two_conversations_with_distinct_titles_are_named_by_topic(tmp_path, all_alive):
    """When titles differ, name by what the conversation is ABOUT, not its
    age -- age tells the user nothing about which is which. Measured live:
    two webapp-fresh conversations, one about a status review and
    one about an architecture handoff."""
    root = tmp_path / ".claude"
    me = os.getpid()
    cwd = "/Users/e/Desktop/webapp-fresh"
    write_roster(root, pid=me, session_id="s1", cwd=cwd, name="secf-1",
                 started_at=1_788_000_000_000)
    write_roster(root, pid=me + 1, session_id="s2", cwd=cwd, name="secf-2",
                 started_at=1_000_000_000_000)
    write_transcript(root, cwd=cwd, session_id="s1",
                     title="App development status review", last_prompt="P")
    write_transcript(root, cwd=cwd, session_id="s2",
                     title="Resume search engine coach architecture and handoff",
                     last_prompt="P")

    by_id = {s.session_id: s.voice_name
             for s in sw.build_snapshot(roots=[root]).sessions}

    assert by_id["s1"] == "webapp-fresh, the status review one"
    assert by_id["s2"] == "webapp-fresh, the architecture handoff one"
    assert "newer" not in by_id["s1"] and "older" not in by_id["s2"]


def test_a_missing_title_on_either_side_falls_through_to_state_naming(tmp_path, all_alive):
    """Live shape: one hammer conversation has a title, its sibling has none
    at all -- topic naming cannot tell the whole group apart, so it falls
    through to state, which can."""
    root = tmp_path / ".claude"
    me = os.getpid()
    write_roster(root, pid=me, session_id="s1", cwd="/p/hammer", name="hammer-a",
                 status="waiting", waiting_for="input needed")
    write_roster(root, pid=me + 1, session_id="s2", cwd="/p/hammer", name="hammer-b",
                 status="idle")
    write_transcript(root, cwd="/p/hammer", session_id="s1",
                     title="Review and improve the hammer worker",
                     last_prompt="P")
    write_transcript(root, cwd="/p/hammer", session_id="s2", title=None,
                     last_prompt="version")

    by_id = {s.session_id: s.voice_name
             for s in sw.build_snapshot(roots=[root]).sessions}

    assert by_id["s1"] == "the hammer that needs you"
    assert by_id["s2"] == "the hammer that's idle"


def test_identical_titles_also_fall_through_to_state_naming(tmp_path, all_alive):
    """Two titles that are both non-empty but identical are just as
    undistinguishing as a missing one -- must fall through the same way."""
    root = tmp_path / ".claude"
    me = os.getpid()
    write_roster(root, pid=me, session_id="s1", cwd="/p/chitauri", name="chitauri-a",
                 status="busy")
    write_roster(root, pid=me + 1, session_id="s2", cwd="/p/chitauri", name="chitauri-b",
                 status="idle")
    for sid in ("s1", "s2"):
        write_transcript(root, cwd="/p/chitauri", session_id=sid, title="Same title",
                         last_prompt="P")

    by_id = {s.session_id: s.voice_name
             for s in sw.build_snapshot(roots=[root]).sessions}

    assert by_id["s1"] == "the chitauri that's working"
    assert by_id["s2"] == "the chitauri that's idle"


def test_identical_titles_and_identical_states_fall_all_the_way_to_age(tmp_path, all_alive):
    """When NEITHER topic nor state distinguishes the group, age is still
    the last resort it always was."""
    root = tmp_path / ".claude"
    me = os.getpid()
    write_roster(root, pid=me, session_id="s1", cwd="/p/chitauri", name="chitauri-a",
                 status="idle", started_at=1_000_000_000_000)
    write_roster(root, pid=me + 1, session_id="s2", cwd="/p/chitauri", name="chitauri-b",
                 status="idle", started_at=1_788_000_000_000)
    for sid in ("s1", "s2"):
        write_transcript(root, cwd="/p/chitauri", session_id=sid, title="Same title",
                         last_prompt="P")

    by_id = {s.session_id: s.voice_name
             for s in sw.build_snapshot(roots=[root]).sessions}

    assert by_id == {"s1": "the older chitauri", "s2": "the newer chitauri"}


def test_every_generated_voice_name_resolves_to_exactly_one_session(tmp_path, all_alive):
    """The whole point of naming by topic/state instead of age: every name
    JARVIS speaks must be a name the user can hand straight back and have it
    resolve unambiguously -- otherwise this rebuilds the dead-end
    disambiguation loop that was already fixed once. Build one snapshot that
    mixes every naming strategy (topic, state, and age) plus a lone
    conversation, and check the property holds for every generated name."""
    root = tmp_path / ".claude"
    me = os.getpid()
    pids = iter(range(me, me + 100))

    # topic-named pair
    secf_cwd = "/Users/e/Desktop/webapp-fresh"
    write_roster(root, pid=next(pids), session_id="secf1", cwd=secf_cwd, name="secf-1")
    write_roster(root, pid=next(pids), session_id="secf2", cwd=secf_cwd, name="secf-2")
    write_transcript(root, cwd=secf_cwd, session_id="secf1",
                     title="App development status review", last_prompt="P")
    write_transcript(root, cwd=secf_cwd, session_id="secf2",
                     title="Resume search engine coach architecture and handoff",
                     last_prompt="P")

    # state-named pair (title missing on one side)
    write_roster(root, pid=next(pids), session_id="h1", cwd="/p/hammer", name="hammer-a",
                 status="waiting", waiting_for="input needed")
    write_roster(root, pid=next(pids), session_id="h2", cwd="/p/hammer", name="hammer-b",
                 status="idle")
    write_transcript(root, cwd="/p/hammer", session_id="h1",
                     title="Review and improve the hammer worker",
                     last_prompt="P")
    write_transcript(root, cwd="/p/hammer", session_id="h2", title=None,
                     last_prompt="version")

    # age-named pair (identical titles AND identical states)
    write_roster(root, pid=next(pids), session_id="c1", cwd="/p/chitauri", name="chitauri-a",
                 started_at=1_000_000_000_000)
    write_roster(root, pid=next(pids), session_id="c2", cwd="/p/chitauri", name="chitauri-b",
                 started_at=1_788_000_000_000)
    for sid in ("c1", "c2"):
        write_transcript(root, cwd="/p/chitauri", session_id=sid, title="Same",
                         last_prompt="P")

    # lone conversation
    write_roster(root, pid=next(pids), session_id="lone", cwd="/p/solo", name="solo-1")
    write_transcript(root, cwd="/p/solo", session_id="lone", title="T", last_prompt="P")

    snap = sw.build_snapshot(roots=[root])

    voice_names = [s.voice_name for s in snap.sessions]
    assert len(voice_names) == len(set(voice_names)), "voice names must be unique"

    for s in snap.sessions:
        got = snap.resolve(s.voice_name)
        assert [x.session_id for x in got] == [s.session_id], \
            f"{s.voice_name!r} must resolve to exactly {s.session_id!r}, got {got}"


def test_a_voice_name_never_contains_the_roster_suffix(tmp_path, all_alive):
    root = tmp_path / ".claude"
    write_roster(root, pid=os.getpid(), session_id="s",
                 cwd="/Users/e/Desktop/webapp-fresh",
                 name="webapp-fresh-9b")
    write_transcript(root, cwd="/Users/e/Desktop/webapp-fresh",
                     session_id="s", title="T", last_prompt="P")

    assert sw.build_snapshot(roots=[root]).sessions[0].voice_name == \
        "webapp-fresh"


# --- resolve -----------------------------------------------------------------

def _two_hammers(tmp_path):
    """Two hammer conversations in one folder plus a lone chitauri. Requires the
    `all_alive` fixture: the pids here are placeholders, not real processes."""
    root = tmp_path / ".claude"
    me = os.getpid()
    write_roster(root, pid=me, session_id="h1", cwd="/p/hammer", name="hammer-4b",
                 started_at=1_000_000_000_000)
    write_roster(root, pid=me + 1, session_id="h2", cwd="/p/hammer", name="hammer-18",
                 started_at=1_788_000_000_000)
    write_roster(root, pid=me + 2, session_id="c1", cwd="/p/chitauri", name="chitauri-7e")
    for cwd, sid in (("/p/hammer", "h1"), ("/p/hammer", "h2"), ("/p/chitauri", "c1")):
        write_transcript(root, cwd=cwd, session_id=sid, title="T", last_prompt="P")
    return sw.build_snapshot(roots=[root])


def test_resolve_returns_one_match_for_an_unambiguous_name(tmp_path, all_alive):
    snap = _two_hammers(tmp_path)
    assert [s.session_id for s in snap.resolve("chitauri")] == ["c1"]
    assert [s.session_id for s in snap.resolve("the chitauri one")] == ["c1"]


def test_resolve_returns_every_candidate_when_ambiguous_it_never_guesses(tmp_path, all_alive):
    """JARVIS must ask, not pick .first — getting this wrong steers the wrong
    session, which is unrecoverable."""
    snap = _two_hammers(tmp_path)
    matches = snap.resolve("hammer")
    assert len(matches) == 2
    assert {s.session_id for s in matches} == {"h1", "h2"}


def test_resolve_disambiguates_on_the_voice_name(tmp_path, all_alive):
    snap = _two_hammers(tmp_path)
    assert [s.session_id for s in snap.resolve("the newer hammer")] == ["h2"]


def test_that_one_resolves_to_the_last_mentioned_session(tmp_path, all_alive):
    snap = _two_hammers(tmp_path)
    assert [s.session_id for s in snap.resolve("that one", last_mentioned="h1")] == ["h1"]


def test_resolve_of_an_unknown_name_returns_nothing(tmp_path, all_alive):
    assert _two_hammers(tmp_path).resolve("nonexistent") == []


def test_resolve_prefers_exact_project_match_over_a_substring_sibling(tmp_path, all_alive):
    """Verified live: with two `hammer` conversations plus a separate
    `hammer-private` project, plain substring matching made "hammer" a hit
    inside "hammer-private" too, offering an unrelated sibling project.
    `resolve("hammer")` must return only the two real hammers, and
    `resolve("hammer-private")` must return only the one — never 3 and never
    narrowed to 1 for "hammer"."""
    root = tmp_path / ".claude"
    me = os.getpid()
    write_roster(root, pid=me, session_id="h1", cwd="/p/hammer", name="hammer-4b",
                 started_at=1_000_000_000_000)
    write_roster(root, pid=me + 1, session_id="h2", cwd="/p/hammer", name="hammer-18",
                 started_at=1_788_000_000_000)
    write_roster(root, pid=me + 2, session_id="hp", cwd="/p/hammer-private",
                 name="hammer-private-1")
    for cwd, sid in (("/p/hammer", "h1"), ("/p/hammer", "h2"), ("/p/hammer-private", "hp")):
        write_transcript(root, cwd=cwd, session_id=sid, title="T", last_prompt="P")

    snap = sw.build_snapshot(roots=[root])

    assert {s.session_id for s in snap.resolve("hammer")} == {"h1", "h2"}
    assert [s.session_id for s in snap.resolve("hammer-private")] == ["hp"]


def test_resolve_finds_a_worktree_session_by_its_real_repo_name(tmp_path, all_alive):
    """End-to-end: a session whose cwd is a Claude Code worktree must be
    reachable by the repo's real name, not by the worktree/branch directory
    name. Verified live: `resolve("jarvis")` on a worktree cwd returned []
    before this fix."""
    root = tmp_path / ".claude"
    cwd = "/Users/e/Projects/jarvis/.claude/worktrees/runs-dashboard"
    write_roster(root, pid=os.getpid(), session_id="s", cwd=cwd, name="runs-dashboard-1")
    write_transcript(root, cwd=cwd, session_id="s", title="T", last_prompt="P")

    snap = sw.build_snapshot(roots=[root])

    assert snap.sessions[0].project == "jarvis"
    assert [s.session_id for s in snap.resolve("jarvis")] == ["s"]


def test_by_project_groups_conversations_the_way_a_person_talks(tmp_path, all_alive):
    snap = _two_hammers(tmp_path)
    groups = snap.by_project()
    assert sorted(groups) == ["chitauri", "hammer"]
    assert len(groups["hammer"]) == 2 and len(groups["chitauri"]) == 1


# --- resolve prefers real conversations over fresh ones -----------------------

def test_resolve_prefers_the_one_real_conversation_over_two_fresh_siblings(tmp_path, all_alive):
    """Live bug: `tool_session_detail({"name": "chitauri"})` offered 3
    candidates for "chitauri" -- one real conversation the user actually
    worked in, plus two `fresh` conversations nobody has ever prompted.
    Fresh conversations are noise here and must not turn an answerable
    question into a clarifying one."""
    root = tmp_path / ".claude"
    me = os.getpid()
    write_roster(root, pid=me, session_id="real", cwd="/Users/e/Projects/chitauri",
                 name="chitauri-1")
    write_transcript(root, cwd="/Users/e/Projects/chitauri", session_id="real",
                     title="T", last_prompt="P")
    write_roster(root, pid=me + 1, session_id="fresh1", cwd="/Users/e/Desktop/chitauri",
                 name="chitauri-2")
    write_roster(root, pid=me + 2, session_id="fresh2", cwd="/Users/e/Desktop2/chitauri",
                 name="chitauri-3")
    # no write_transcript for fresh1/fresh2 -> they stay `fresh`

    snap = sw.build_snapshot(roots=[root])
    assert snap.by_id("real").state != sw.FRESH
    assert snap.by_id("fresh1").state == sw.FRESH
    assert snap.by_id("fresh2").state == sw.FRESH

    assert [s.session_id for s in snap.resolve("chitauri")] == ["real"]


def test_resolve_still_asks_when_ambiguous_among_real_conversations_plus_a_fresh_one(tmp_path, all_alive):
    """Two REAL hammers plus a fresh third must still return both real ones --
    ambiguity among real conversations must never be silently narrowed."""
    root = tmp_path / ".claude"
    me = os.getpid()
    write_roster(root, pid=me, session_id="h1", cwd="/p/hammer", name="hammer-4b",
                 started_at=1_000_000_000_000)
    write_roster(root, pid=me + 1, session_id="h2", cwd="/p/hammer", name="hammer-18",
                 started_at=1_788_000_000_000)
    write_transcript(root, cwd="/p/hammer", session_id="h1", title="T", last_prompt="P")
    write_transcript(root, cwd="/p/hammer", session_id="h2", title="T", last_prompt="P")
    write_roster(root, pid=me + 2, session_id="h3fresh", cwd="/other/hammer",
                 name="hammer-9")
    # no transcript for h3fresh -> fresh

    snap = sw.build_snapshot(roots=[root])
    assert snap.by_id("h3fresh").state == sw.FRESH

    matches = snap.resolve("hammer")
    assert {s.session_id for s in matches} == {"h1", "h2"}


def test_resolve_returns_all_fresh_matches_when_nothing_real_matches(tmp_path, all_alive):
    """If every match is fresh, the user may genuinely mean a fresh session --
    answering "I don't see it" would be wrong."""
    root = tmp_path / ".claude"
    me = os.getpid()
    write_roster(root, pid=me, session_id="fresh1", cwd="/Users/e/Desktop/chitauri",
                 name="chitauri-2")
    write_roster(root, pid=me + 1, session_id="fresh2", cwd="/Users/e/Desktop2/chitauri",
                 name="chitauri-3")

    snap = sw.build_snapshot(roots=[root])
    assert {s.session_id for s in snap.resolve("chitauri")} == {"fresh1", "fresh2"}


# --- needing_you orders by recency, not age ------------------------------------

def test_needing_you_orders_by_since_most_recent_first(tmp_path, all_alive):
    """Review finding 5: "What needs me?" must list waiting conversations by
    recency (`since`, when the CURRENT STATE began) — never by `started`
    (when the conversation began). Build one conversation that STARTED long
    ago but has only just begun waiting, and one that started recently but
    has been waiting far longer, so ordering by the wrong field would flip
    the result."""
    root = tmp_path / ".claude"
    me = os.getpid()
    # old_starter: started long ago, but just started waiting (since is BIG/late)
    write_roster(root, pid=me, session_id="old_starter", cwd="/p/old",
                 name="old", status="waiting", waiting_for="input needed",
                 started_at=1_000_000_000_000, status_updated_at=1_800_000_000_000)
    # new_starter: started recently, but has been waiting since long ago
    # (since is SMALL/early) -- must be listed AFTER old_starter.
    write_roster(root, pid=me + 1, session_id="new_starter", cwd="/p/new",
                 name="new", status="waiting", waiting_for="input needed",
                 started_at=1_790_000_000_000, status_updated_at=1_100_000_000_000)
    for cwd, sid in (("/p/old", "old_starter"), ("/p/new", "new_starter")):
        write_transcript(root, cwd=cwd, session_id=sid, title="T", last_prompt="P")

    snap = sw.build_snapshot(roots=[root])
    ordered = snap.needing_you()

    assert [s.session_id for s in ordered] == ["old_starter", "new_starter"], \
        "must be ordered by `since` (most-recently-waiting first), not `started`"


# --- the question heuristic -------------------------------------------------
#
# Found on the live machine: a session that had hit its spend limit ended with
# ".../settings/usage?from=cc_cli_limit_message" and was announced as needing
# the user. Needs-you interrupts at URGENT priority, so a false positive here
# nags about a session that wants nothing.

@pytest.mark.parametrize("label,text,expected", [
    ("a spend-limit notice ending in a URL",
     "You've hit your monthly spend limit - raise it at "
     "https://claude.ai/settings/usage?from=cc_cli_limit_message", False),
    ("a URL mid-sentence",
     "See https://example.com/x?y=1 for details, then carry on.", False),
    ("a bare www URL", "Docs at www.example.com/a?b=2", False),
    ("a real question", "Shall I use Postgres or SQLite?", True),
    ("a question followed by options",
     "Which database?\n1. Postgres\n2. SQLite", True),
    ("a plain statement", "Done and pushed to the private repo.", False),
    ("nothing at all", "", False),
])
def test_only_a_real_question_counts_as_needing_you(label, text, expected):
    assert sw._looks_like_a_question(text) is expected, label


# --- answering our own disambiguation question ------------------------------
#
# Found live: JARVIS asked "the newer or the older?", the user said "the newer
# one", and resolve returned BOTH again — the project name is a substring of
# every one of its own voice names. The ask-which-one flow was a dead end and
# JARVIS said he could not pick.

def _two_named(tmp_path, project, cwd="/p/proj"):
    root = tmp_path / ".claude"
    write_roster(root, pid=LIVE_A, session_id="s1", cwd=cwd, name=f"{project}-a",
                 started_at=1_000_000_000_000)
    write_roster(root, pid=LIVE_B, session_id="s2", cwd=cwd, name=f"{project}-b",
                 started_at=1_788_000_000_000)
    for sid in ("s1", "s2"):
        write_transcript(root, cwd=cwd, session_id=sid, title="T", last_prompt="P")
    return sw.build_snapshot(roots=[root])


def test_a_qualified_answer_picks_one(tmp_path, all_alive):
    snap = _two_named(tmp_path, "webapp-fresh",
                      cwd="/Users/e/Desktop/webapp-fresh")

    got = snap.resolve("newer webapp-fresh")

    assert [s.voice_name for s in got] == ["the newer webapp-fresh"]


def test_a_qualified_answer_with_filler_words_still_picks_one(tmp_path, all_alive):
    snap = _two_named(tmp_path, "webapp-fresh",
                      cwd="/Users/e/Desktop/webapp-fresh")

    got = snap.resolve("let's go with the newer one in webapp-fresh")

    assert [s.voice_name for s in got] == ["the newer webapp-fresh"]


# --- excluding JARVIS's own brain from the roster ----------------------------
#
# Measured live: the brain process registers in the roster like any Claude
# Code session -- `entrypoint="sdk-cli"`, cwd = the brain home directory,
# name "jarvis" -- and without this exclusion it turned up as one of the
# user's own conversations, exactly the bug spawned runs had before they were
# filtered out (tests/test_runs_are_not_conversations.py).

def test_brain_cwd_matches_data_paths_brain_home(monkeypatch, tmp_path):
    """`sw._brain_cwd()` reimplements `data_paths.brain_home()` without its
    `mkdir` side effect, because session_watch polls once a second and must
    stay pure filesystem *reading*. Kept honest against the real thing."""
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import data_paths
    assert sw._brain_cwd() == str(data_paths.brain_home())


def test_the_brains_own_process_is_excluded_from_the_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / ".claude"
    brain_cwd = str(tmp_path / "data" / "jarvis")
    write_roster(root, pid=os.getpid(), session_id="brain", cwd=brain_cwd,
                 name="jarvis", entrypoint="sdk-cli")
    write_roster(root, pid=os.getpid() + 1, session_id="real",
                 cwd="/Users/e/Projects/jarvis/.claude/worktrees/runs-dashboard",
                 name="jarvis-94")
    write_transcript(root, cwd="/Users/e/Projects/jarvis/.claude/worktrees/runs-dashboard",
                     session_id="real", title="T", last_prompt="P")

    snap = sw.build_snapshot(roots=[root])

    assert [s.session_id for s in snap.sessions] == ["real"]


def test_a_real_project_named_jarvis_is_not_excluded_by_name_alone(tmp_path, monkeypatch):
    """The cwd match is the deciding signal, checked before any name check --
    this user has a real project called "jarvis", and a name-based check
    would wrongly sweep that conversation up too."""
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / ".claude"
    cwd = "/Users/e/Projects/jarvis/.claude/worktrees/runs-dashboard"
    write_roster(root, pid=os.getpid(), session_id="user-jarvis", cwd=cwd, name="jarvis")
    write_transcript(root, cwd=cwd, session_id="user-jarvis", title="T", last_prompt="P")

    snap = sw.build_snapshot(roots=[root])

    assert [s.session_id for s in snap.sessions] == ["user-jarvis"]


def test_the_brain_cwd_alone_without_sdk_cli_entrypoint_is_not_excluded(tmp_path, monkeypatch):
    """A terminal `claude` session that happens to be run FROM the brain
    home directory (unlikely, but not impossible) is not the brain unless it
    was also launched the way the brain is launched."""
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / ".claude"
    brain_cwd = str(tmp_path / "data" / "jarvis")
    write_roster(root, pid=os.getpid(), session_id="not-the-brain", cwd=brain_cwd,
                 name="jarvis", entrypoint="cli")
    write_transcript(root, cwd=brain_cwd, session_id="not-the-brain",
                     title="T", last_prompt="P")

    snap = sw.build_snapshot(roots=[root])

    assert [s.session_id for s in snap.sessions] == ["not-the-brain"]


def test_an_unqualified_name_still_asks(tmp_path, all_alive):
    """The safety property: ambiguity among real conversations must still
    return every candidate so the caller asks."""
    snap = _two_named(tmp_path, "webapp-fresh",
                      cwd="/Users/e/Desktop/webapp-fresh")

    assert len(snap.resolve("webapp-fresh")) == 2


# ── one bad byte must not freeze the whole roster ──────────────────────────
#
# `recap_from` stringified the value to TEST it and then passed the raw one
# to `_clip`, which does `.split()`. A non-string `text` raised
# AttributeError, escaped to `SessionWatcher._loop`, was logged as a warning
# and swallowed — and `self.snapshot` then FROZE at the last good poll for as
# long as that line stayed inside the 64 KB tail, while `/api/sessions` went
# on answering 200 with stale data. The sibling fields are `isinstance`-
# guarded; this one was the outlier.

@pytest.mark.parametrize("bad", [5, None, {"nested": "object"}, ["a", "list"],
                                 True, 1.5])
def test_a_non_string_text_block_does_not_raise(bad):
    line = {"type": "assistant", "isSidechain": False,
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": bad}]}}

    recap = sw.recap_from([line])

    assert recap.exists is True
    assert recap.last_text is None or isinstance(recap.last_text, str)


@pytest.mark.parametrize("field,key", [("aiTitle", "ai-title"),
                                       ("lastPrompt", "last-prompt")])
def test_a_non_string_title_or_prompt_does_not_raise(field, key):
    assert sw.recap_from([{"type": key, field: 7}]).exists is True


def test_one_bad_line_does_not_freeze_the_snapshot(tmp_path, monkeypatch):
    """The visible failure, end to end: a poll that raises leaves the LAST
    snapshot standing and nothing on the page says so."""
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / ".claude"
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p/one", name="one")
    path = write_transcript(root, cwd="/p/one", session_id="s",
                            title="T", last_prompt="P")
    lines = path.read_text().splitlines()
    lines.append(json.dumps({
        "type": "assistant", "isSidechain": False, "sessionId": "s",
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": 12345}]}}))
    path.write_text("\n".join(lines) + "\n")

    watcher = sw.SessionWatcher(roots=[root])
    watcher.poll_once()

    assert [s.session_id for s in watcher.snapshot.sessions] == ["s"]
    assert watcher.snapshot.taken_at > 0


# ── a capped count is a floor in BOTH numbers ──────────────────────────────

def _write_agents(root, cwd, session_id, count, *, recent):
    d = root / "projects" / sw.encode_cwd(cwd) / session_id / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    old = 1788404571.0 - 10 * 3600
    for i in range(count):
        f = d / f"agent-{i:04d}.jsonl"
        f.write_text("{}\n")
        if i not in recent:
            os.utime(f, (old, old))
    return d


def test_agents_active_is_a_floor_whenever_the_cap_was_hit(tmp_path):
    """`capped` is computed from the FULL listing but `active` is counted
    inside `transcripts[:MAX_AGENT_FILES]`, and that slice is by FILENAME —
    uncorrelated with recency. So when the cap is hit, `active` is a floor
    exactly as `seen` is, and the caller has to be told so.
    """
    now = 1788404571.0
    root = tmp_path / ".claude"
    total = sw.MAX_AGENT_FILES + 5
    # Every file recent: the true active count is `total`, and no slice of
    # 300 of them can say so.
    _write_agents(root, "/p/one", "s", total, recent=set(range(total)))

    seen, active, capped = sw.count_agents(root, "/p/one", "s", now)

    assert capped is True
    assert seen == sw.MAX_AGENT_FILES
    assert active <= sw.MAX_AGENT_FILES
    assert active < total, "the slice cannot have seen them all"


def test_an_uncapped_active_count_is_exact(tmp_path):
    now = 1788404571.0
    root = tmp_path / ".claude"
    _write_agents(root, "/p/one", "s", 5, recent={0, 1})

    seen, active, capped = sw.count_agents(root, "/p/one", "s", now)

    assert (seen, active, capped) == (5, 2, False)


# ── a transcript is looked for under EVERY root the session registered in ──

def test_a_transcript_under_the_second_root_is_still_found(tmp_path):
    """Measured shape: a session can register in both roots, and its
    transcript live under only one of them. `_pick_primary` chooses by
    liveness and recency, not by which root has the file, so looking only
    under `primary.root` reported a busy conversation as `fresh` — "not
    started" — which is the one state JARVIS never announces.
    """
    a, b = tmp_path / ".claude", tmp_path / ".claude-orcha"
    write_roster(a, pid=os.getpid(), session_id="s", cwd="/p/one", name="one",
                 status="working")
    write_roster(b, pid=os.getpid() - 1, session_id="s", cwd="/p/one",
                 name="one", status="working")
    # The transcript exists under the root whose entry is NOT primary.
    write_transcript(b, cwd="/p/one", session_id="s",
                     title="Real work", last_prompt="do the thing")

    snap = sw.build_snapshot(roots=[a, b])

    session, = snap.sessions
    assert session.state != sw.FRESH, "a working conversation read as 'not started'"
    assert session.title == "Real work"


def test_subagents_under_the_second_root_are_still_counted(tmp_path):
    a, b = tmp_path / ".claude", tmp_path / ".claude-orcha"
    write_roster(a, pid=os.getpid(), session_id="s", cwd="/p/one", name="one")
    write_roster(b, pid=os.getpid() - 1, session_id="s", cwd="/p/one",
                 name="one")
    write_transcript(a, cwd="/p/one", session_id="s", title="T",
                     last_prompt="P")
    _write_agents(b, "/p/one", "s", 3, recent={0, 1, 2})

    session, = sw.build_snapshot(roots=[a, b]).sessions

    assert session.agents_seen == 3


# ── the brain is the brain however the path is spelled ─────────────────────

def test_a_symlinked_data_dir_does_not_turn_the_brain_into_a_conversation(
        tmp_path, monkeypatch):
    """`_is_own_brain` compared paths textually. A symlinked
    `JARVIS_DATA_DIR` — the ordinary shape when data lives on another
    volume — spells the same directory two ways, and the brain then appeared
    in the roster as one of the user's own conversations.
    """
    real = tmp_path / "real-data"
    real.mkdir()
    link = tmp_path / "linked-data"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("JARVIS_DATA_DIR", str(link))
    brain_home = sw.brain_cwd()
    Path(brain_home).mkdir(parents=True, exist_ok=True)
    # The process registers the path it actually resolved to.
    through_the_link = str(Path(brain_home).resolve())

    root = tmp_path / ".claude"
    write_roster(root, pid=os.getpid(), session_id="the-brain",
                 cwd=through_the_link, name="jarvis", entrypoint="sdk-cli")
    write_transcript(root, cwd=through_the_link, session_id="the-brain",
                     title="T", last_prompt="P")

    snap = sw.build_snapshot(roots=[root])

    assert snap.sessions == [], (
        "JARVIS's own brain was listed as one of the user's conversations")


# ── names and badges are re-derived once the runs are gone ────────────────

def test_excluding_runs_renames_what_is_left(tmp_path, monkeypatch):
    """Measured 2026-09-04: a `claude -p` run registers
    `entrypoint: "sdk-cli"`, `kind: "interactive"`, so `_origin` calls it
    "background" and it can never take the "main" badge. It CAN still change
    the names though: `_assign_voice_names` ran over the whole roster, so a
    user with one conversation in a project where a run had also touched
    down was told about "the newer" and "the older" one — and then only one
    of them survived the filter.
    """
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / ".claude"
    run_id = "11111111-2222-4333-8444-555555555555"
    write_roster(root, pid=os.getpid(), session_id="the-users-window",
                 cwd="/p/tsw", name="tsw", status="idle")
    write_roster(root, pid=os.getpid() - 1, session_id=run_id, cwd="/p/tsw",
                 name="tsw", entrypoint="sdk-cli", status="idle")
    for sid in ("the-users-window", run_id):
        write_transcript(root, cwd="/p/tsw", session_id=sid, title="T",
                         last_prompt="P")

    snap = sw.build_snapshot(roots=[root])
    assert len(snap.sessions) == 2

    kept = snap.excluding({run_id})

    assert [s.session_id for s in kept.sessions] == ["the-users-window"]
    assert kept.sessions[0].voice_name == "tsw", (
        "the surviving conversation kept a name that only made sense beside "
        "a run the user never saw")
    assert kept.taken_at == snap.taken_at


def test_excluding_nothing_is_the_same_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / ".claude"
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p/one", name="one")
    write_transcript(root, cwd="/p/one", session_id="s", title="T",
                     last_prompt="P")

    snap = sw.build_snapshot(roots=[root])

    assert snap.excluding(set()) is snap
    assert snap.excluding({"not-here"}) is snap
