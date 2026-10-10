"""Who answers a session's prompts — and why a program's session never
"needs you" the moment it pauses for one.

Measured live, 2026-09-28. Paperclip started a Claude Code session in
stark-armory-next through the Agent SDK: roster `entrypoint: "sdk-ts"`,
parent process `claude-agent-acp`, CLI flags `--permission-prompt-tool
stdio`. Every Bash call it made asked Paperclip for permission over stdio,
and Paperclip — whose ACP engine defaults to `approve-all` — answered each
one itself. Sampled at 20 Hz, the four waits lasted 0.20, 2.34, 0.21 and
0.06 seconds. JARVIS polls once a second, caught six such waits in seven
minutes, and announced every one at URGENT priority:

    stark-armory-next is waiting on a permission prompt, sir —
    that one needs your own keystroke.

There was no terminal and no keystroke to give. The user asked where it was.

Two faults, one root. `_origin` knew four entrypoints and called every other
one — `sdk-ts` included — a "terminal", so the session was also crowned "the
only live conversation here" and main. And `_derive_state` treated every
wait as the user's, whoever the session belonged to.

The rule now: a session a PROGRAM started (the SDK, `claude -p`, an MCP
client, a CI action) waits on that program. Its prompts are the program's
to answer, and it is `working` while the program answers. Only when the
program has sat on one for `HOST_ANSWER_GRACE_SEC` does it become the
user's business — once, and said as the program's prompt, not a keystroke.
"""

import json
import os

import pytest

import session_watch as sw
from tests.fixtures.roster import NOW_MS, write_roster, write_transcript

T0 = NOW_MS / 1000.0             # when the fixture's status last changed


def _one(root, *, entrypoint, status="waiting", waiting_for="permission prompt",
         status_updated_at=NOW_MS, assistant_texts=(), tools=()):
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p/stark",
                 name="stark-52", entrypoint=entrypoint, status=status,
                 waiting_for=waiting_for, status_updated_at=status_updated_at)
    write_transcript(root, cwd="/p/stark", session_id="s",
                     title="daily brief", last_prompt="run the brief",
                     assistant_texts=list(assistant_texts), tools=list(tools))


def _snap(root, *, at):
    s, = sw.build_snapshot(roots=[root], now=at).sessions
    return s


# --- who is at the other end ---------------------------------------------

# Every value in the CLI's own table of entrypoints (`Vin` in the 2.1.270
# binary), and what each one means for who answers its prompts. Taken from
# the binary, not remembered: `sdk-ts` is the one that was missing.
EVERY_KNOWN_ENTRYPOINT = {
    "cli": sw.TERMINAL,
    "claude-desktop": sw.DESKTOP,
    "claude-desktop-3p": sw.DESKTOP,
    "local-agent": sw.DESKTOP,               # Cowork, inside the desktop app
    "local_agent": sw.DESKTOP,               # the CLI's own legacy spelling
    "claude-vscode": sw.EDITOR,
    "remote": sw.REMOTE,
    "remote_baku": sw.REMOTE,
    "remote_cowork": sw.REMOTE,
    "remote_desktop": sw.REMOTE,
    "remote_mobile": sw.REMOTE,
    "ssh-remote": sw.REMOTE,
    "claude_in_slack": sw.REMOTE,
    "claude-in-slack": sw.REMOTE,
    "claude-in-teams": sw.REMOTE,
    "sdk-cli": sw.BACKGROUND,                # `claude -p`
    "sdk-ts": sw.BACKGROUND,                 # the TypeScript Agent SDK
    "sdk-py": sw.BACKGROUND,                 # the Python Agent SDK
    "mcp": sw.BACKGROUND,                    # `claude mcp serve`
    "claude-code-github-action": sw.BACKGROUND,
    "bench": sw.BACKGROUND,
    "remote_trigger": sw.BACKGROUND,         # a scheduled routine
    "remote_cowork_trigger": sw.BACKGROUND,
}


@pytest.mark.parametrize("entrypoint,origin", sorted(EVERY_KNOWN_ENTRYPOINT.items()))
def test_every_entrypoint_the_cli_knows_has_an_origin(tmp_path, entrypoint, origin):
    root = tmp_path / ".claude"
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n",
                 entrypoint=entrypoint)
    entry, = sw.read_roster([root])
    assert sw._origin(entry) == origin


def test_an_unrecognised_entrypoint_is_not_called_a_terminal(tmp_path):
    """The old default. Anything the table does not know was announced as a
    terminal that wanted a keystroke; it is `other` now, and nothing true
    about a terminal is claimed for it."""
    root = tmp_path / ".claude"
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n",
                 entrypoint="claude-something-new")
    entry, = sw.read_roster([root])
    assert sw._origin(entry) == sw.OTHER


def test_a_roster_entry_with_no_entrypoint_is_a_terminal(tmp_path):
    """Older CLIs wrote no `entrypoint` at all; `_parse_entry` has always read
    that as `cli`, and a terminal is what those sessions were."""
    root = tmp_path / ".claude"
    p = write_roster(root, pid=os.getpid(), session_id="s", cwd="/p", name="n")
    data = json.loads(p.read_text(encoding="utf-8"))
    del data["entrypoint"]
    p.write_text(json.dumps(data), encoding="utf-8")
    entry, = sw.read_roster([root])
    assert sw._origin(entry) == sw.TERMINAL


def test_the_origins_split_into_people_and_programs():
    """One partition, stated once, so no caller has to decide it again."""
    assert sw.ATTENDED_ORIGINS == {sw.TERMINAL, sw.DESKTOP, sw.EDITOR, sw.REMOTE}
    assert sw.PROGRAM_ORIGINS == {sw.BACKGROUND}
    assert not sw.ATTENDED_ORIGINS & sw.PROGRAM_ORIGINS
    assert sw.OTHER not in sw.ATTENDED_ORIGINS | sw.PROGRAM_ORIGINS


# --- the reported failure --------------------------------------------------

def test_the_reported_session_is_working_while_its_host_answers(tmp_path):
    """stark-armory-next, exactly: `sdk-ts`, waiting on a permission
    prompt, one second in."""
    root = tmp_path / ".claude"
    _one(root, entrypoint="sdk-ts")

    s = _snap(root, at=T0 + 1)

    assert s.origin == sw.BACKGROUND
    assert s.state == sw.WORKING, "its host is answering; nothing is asked of you"
    assert s.needs is None
    assert s.needs_a_human_hand is False
    assert s.waiting_on_host == "permission prompt", (
        "the pause is still visible — as the host's, not the user's")


@pytest.mark.parametrize("entrypoint", ["sdk-cli", "sdk-py", "mcp",
                                        "claude-code-github-action"])
def test_every_program_s_prompt_is_its_host_s(tmp_path, entrypoint):
    root = tmp_path / ".claude"
    _one(root, entrypoint=entrypoint)
    s = _snap(root, at=T0 + 1)
    assert (s.state, s.needs, s.waiting_on_host) == (
        sw.WORKING, None, "permission prompt")


@pytest.mark.parametrize("reason", ["input needed", "dialog open", None])
def test_every_kind_of_wait_in_a_program_s_session_is_its_host_s(tmp_path, reason):
    """Under the SDK there is no screen for a dialog to open on and no
    person for input to come from: whatever it waits on, it waits on the
    program. A bare `waiting` with no reason is the same wait unnamed."""
    root = tmp_path / ".claude"
    _one(root, entrypoint="sdk-ts", waiting_for=reason)
    s = _snap(root, at=T0 + 1)
    assert s.state == sw.WORKING
    assert s.waiting_on_host == (reason or "")


def test_the_grace_is_far_longer_than_any_answer_a_host_was_measured_giving():
    """2.34 s was the longest of the waits measured live. The grace is what
    stands between a host doing its job and the user being interrupted, so
    it must sit well clear of that — and it must still end, or a host that
    hangs would hide its session from the user for good."""
    assert sw.HOST_ANSWER_GRACE_SEC >= 10 * 2.34
    assert sw.HOST_ANSWER_GRACE_SEC <= 300


def test_a_prompt_a_host_sits_on_past_the_grace_becomes_the_user_s(tmp_path):
    """A host with a person behind it (an editor speaking ACP), or a host
    that has hung, leaves the prompt standing. After the grace the user is
    told — which is the only thing that can still move it."""
    root = tmp_path / ".claude"
    _one(root, entrypoint="sdk-ts")

    s = _snap(root, at=T0 + sw.HOST_ANSWER_GRACE_SEC + 1)

    assert s.state == sw.NEEDS_YOU
    assert s.needs == "permission prompt"
    assert s.waiting_on_host is None, "one of the two, never both"
    assert s.origin == sw.BACKGROUND, "and it is still the program's prompt"


def test_the_grace_is_measured_from_when_the_wait_began(tmp_path):
    root = tmp_path / ".claude"
    _one(root, entrypoint="sdk-ts")
    just_inside = _snap(root, at=T0 + sw.HOST_ANSWER_GRACE_SEC - 1)
    assert just_inside.state == sw.WORKING


def test_a_wait_with_no_stamp_stays_the_host_s(tmp_path):
    """No `statusUpdatedAt` means no way to say how long the host has had
    it. Absence of evidence is not a long wait; the user is not interrupted
    on a guess."""
    root = tmp_path / ".claude"
    _one(root, entrypoint="sdk-ts", status_updated_at=None)
    s = _snap(root, at=T0 + 10_000)
    assert (s.state, s.waiting_on_host) == (sw.WORKING, "permission prompt")


@pytest.mark.parametrize("entrypoint", ["cli", "claude-desktop", "claude-vscode",
                                        "remote_mobile", "claude-something-new"])
def test_a_person_s_session_still_needs_you_at_once(tmp_path, entrypoint):
    """The grace is for programs only. A prompt in a session a person is
    sitting at — or one JARVIS cannot place — is announced immediately, as
    it always was."""
    root = tmp_path / ".claude"
    _one(root, entrypoint=entrypoint)
    s = _snap(root, at=T0 + 1)
    assert (s.state, s.needs, s.waiting_on_host) == (
        sw.NEEDS_YOU, "permission prompt", None)


def test_a_program_s_question_is_for_the_program(tmp_path):
    """An idle SDK session whose last words end in a question asked its
    host, which is what reads its result. JARVIS used to announce that as
    "has stopped and wants you"."""
    root = tmp_path / ".claude"
    _one(root, entrypoint="sdk-ts", status="idle", waiting_for=None,
         assistant_texts=["Brief posted. Shall I file the follow-ups too?"])
    assert _snap(root, at=T0 + 1).state == sw.IDLE


def test_a_program_s_asked_question_tool_is_for_the_program(tmp_path):
    root = tmp_path / ".claude"
    _one(root, entrypoint="sdk-ts", status="idle", waiting_for=None,
         tools=["Bash", "AskUserQuestion"])
    assert _snap(root, at=T0 + 1).state == sw.IDLE


def test_a_person_s_question_still_needs_them(tmp_path):
    root = tmp_path / ".claude"
    _one(root, entrypoint="claude-desktop", status="idle", waiting_for=None,
         assistant_texts=["Shall I file the follow-ups too?"])
    assert _snap(root, at=T0 + 1).state == sw.NEEDS_YOU


# --- what the dashboard and the events carry -------------------------------

def test_the_host_wait_and_the_entrypoint_reach_the_dashboard(tmp_path):
    root = tmp_path / ".claude"
    _one(root, entrypoint="sdk-ts")
    row = sw.session_to_dict(_snap(root, at=T0 + 1))
    assert row["origin"] == "background"
    assert row["entrypoint"] == "sdk-ts"
    assert row["waiting_on_host"] == "permission prompt"
    assert row["state"] == "working" and row["needs"] is None


def test_a_person_s_session_carries_no_host_wait(tmp_path):
    root = tmp_path / ".claude"
    _one(root, entrypoint="cli", status="busy", waiting_for=None)
    row = sw.session_to_dict(_snap(root, at=T0 + 1))
    assert row["waiting_on_host"] is None and row["entrypoint"] == "cli"


# --- which conversation is the main one -----------------------------------

def test_a_program_s_session_is_never_the_main_one_even_alone(tmp_path):
    """The reported session was `primary: true`, "the only live conversation
    here" — main, in a project nobody was typing into."""
    root = tmp_path / ".claude"
    _one(root, entrypoint="sdk-ts", status="busy", waiting_for=None)
    s = _snap(root, at=T0 + 1)
    assert s.primary is False
    assert s.primary_reason == sw.NOT_PRIMARY_BACKGROUND


@pytest.mark.parametrize("entrypoint", ["claude-desktop", "claude-vscode"])
def test_a_desktop_or_editor_session_can_be_the_main_one(tmp_path, entrypoint):
    """Somebody IS typing into these — every session on this machine but
    JARVIS's own and Paperclip's is a desktop one — and every one of them was
    labelled "a background conversation"."""
    root = tmp_path / ".claude"
    _one(root, entrypoint=entrypoint, status="busy", waiting_for=None)
    s = _snap(root, at=T0 + 1)
    assert s.primary is True and s.primary_reason == sw.PRIMARY_ONLY


def test_a_session_started_by_something_unrecognised_is_not_crowned(tmp_path):
    root = tmp_path / ".claude"
    _one(root, entrypoint="claude-something-new", status="busy", waiting_for=None)
    s = _snap(root, at=T0 + 1)
    assert s.primary is False
    assert s.primary_reason == sw.NOT_PRIMARY_UNPLACED


# --- what the watcher announces --------------------------------------------

def _watch(root):
    w = sw.SessionWatcher(roots=[root], interval=0.01)
    events = []
    w.on_event(events.append)
    return w, events


def _status(root, *, status, waiting_for=None, at_ms, entrypoint="sdk-ts"):
    write_roster(root, pid=os.getpid(), session_id="s", cwd="/p/stark",
                 name="stark-52", entrypoint=entrypoint, status=status,
                 waiting_for=waiting_for, status_updated_at=at_ms)


def test_prompts_the_host_answers_are_never_announced(tmp_path):
    """The live sequence: busy, a permission wait the host clears in under
    a second, busy again — over and over. Six URGENT interrupts in seven
    minutes, before; none now."""
    root = tmp_path / ".claude"
    write_transcript(root, cwd="/p/stark", session_id="s", title="daily brief",
                     last_prompt="run the brief")
    w, events = _watch(root)
    t = NOW_MS
    _status(root, status="busy", at_ms=t)
    w.poll_once(now=t / 1000)                                # baseline
    for i in range(1, 7):
        start = t + i * 60_000
        _status(root, status="waiting", waiting_for="permission prompt", at_ms=start)
        w.poll_once(now=start / 1000 + 0.5)
        _status(root, status="busy", at_ms=start + 2_340)
        w.poll_once(now=start / 1000 + 3)
    assert events == []


def test_a_prompt_the_host_sits_on_is_announced_once_after_the_grace(tmp_path):
    root = tmp_path / ".claude"
    write_transcript(root, cwd="/p/stark", session_id="s", title="daily brief",
                     last_prompt="run the brief")
    w, events = _watch(root)
    _status(root, status="busy", at_ms=NOW_MS)
    w.poll_once(now=T0)
    _status(root, status="waiting", waiting_for="permission prompt", at_ms=NOW_MS)
    for dt in (1, 30, sw.HOST_ANSWER_GRACE_SEC - 1):
        w.poll_once(now=T0 + dt)
    assert events == [], "inside the grace it is the host's"

    for dt in (sw.HOST_ANSWER_GRACE_SEC + 1, sw.HOST_ANSWER_GRACE_SEC + 2, 600):
        w.poll_once(now=T0 + dt)

    assert [e["kind"] for e in events] == ["needs_you"]
    said = events[0]["session"]
    assert said["origin"] == "background" and said["needs"] == "permission prompt"


def test_a_prompt_the_host_answers_does_not_swallow_the_finish(tmp_path):
    """`finished` is published on working -> idle. A permission wait in
    between used to make the last state before idle `needs_you`, so a
    program's session that paused for a tool near its end was never said to
    have finished."""
    root = tmp_path / ".claude"
    write_transcript(root, cwd="/p/stark", session_id="s", title="daily brief",
                     last_prompt="run the brief")
    w, events = _watch(root)
    _status(root, status="busy", at_ms=NOW_MS)
    w.poll_once(now=T0)
    _status(root, status="waiting", waiting_for="permission prompt",
            at_ms=NOW_MS + 40_000)
    w.poll_once(now=T0 + 40.5)
    _status(root, status="idle", at_ms=NOW_MS + 42_000)
    w.poll_once(now=T0 + 43)
    assert [e["kind"] for e in events] == ["finished"]


def test_a_person_s_prompt_is_still_announced_at_once(tmp_path):
    root = tmp_path / ".claude"
    write_transcript(root, cwd="/p/stark", session_id="s", title="daily brief",
                     last_prompt="run the brief")
    w, events = _watch(root)
    _status(root, status="busy", at_ms=NOW_MS, entrypoint="claude-desktop")
    w.poll_once(now=T0)
    _status(root, status="waiting", waiting_for="permission prompt",
            at_ms=NOW_MS + 1_000, entrypoint="claude-desktop")
    w.poll_once(now=T0 + 1.5)
    assert [e["kind"] for e in events] == ["needs_you"]
    assert events[0]["session"]["origin"] == "desktop"
