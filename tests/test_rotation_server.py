"""Server-side rotation: the swap happens at a pause, and never silently.

Two things are being pinned here. First, a generation is never replaced
without a trace: the outgoing brain is asked for its own handover, and if it
will not or cannot give one, the server writes a minimal entry itself and
rotates anyway. Second, the request for that handover runs with
origin="system", so the acting-tool gate refuses any write the brain might
attempt while answering it.

No test here spawns a real `claude`: the brain is a fake throughout.
"""

import asyncio

import pytest


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import importlib
    import server as server_module
    importlib.reload(server_module)
    return server_module


class _Brain:
    def __init__(self, pending=True, overdue=False, rotates=True):
        self.rotation_pending = pending
        self.rotation_overdue = overdue
        self.rotated_with = None
        self.rotations = 0
        self.asked = []
        self.ready = True
        # The real Brain exposes this: None means no turn is in flight, which
        # is what makes the moment a pause.
        self.current_origin = None
        self.stopped = False
        self._rotates = rotates
        self.generation = 1

    async def turn(self, text, origin="user", on_delta=None, on_tool=None,
                   on_switch=None):
        self.asked.append((text, origin))
        import brain
        return brain.TurnResult(origin, "We fixed chitauri and Tony chose Postgres.",
                                "result")

    async def rotate(self, handover=None, *, fresh=False, **_):
        self.rotations += 1
        if not self._rotates:
            return False
        self.rotated_with = handover
        self.rotation_pending = False
        self.generation += 1
        return True

    async def stop(self):
        self.stopped = True


class _Clock:
    """The server's rotation clock, driven by hand."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.mark.asyncio
async def test_a_pending_rotation_asks_for_a_journal_then_rotates(wired, monkeypatch):
    server = wired
    b = _Brain()
    monkeypatch.setattr(server, "brain_instance", b)

    await server._maybe_rotate()

    assert b.asked, "the outgoing brain is asked for a handover"
    assert b.asked[0][1] == "system", "the journal request is not a user turn"
    assert "chitauri" in (b.rotated_with or "")

    import jarvis_memory as jm
    assert "chitauri" in (jm.latest_journal() or ""), "the journal is on disk"


class _Speech:
    """Records what was said, without a real SpeechScheduler: this is
    testing that the server WIRES the announcement, not the scheduler's own
    behaviour (that lives in test_speech.py against the real thing)."""

    def __init__(self):
        self.said = []

    async def say(self, text, priority=None, immediate=None):
        self.said.append((text, priority, immediate))


@pytest.mark.asyncio
async def test_a_rotation_is_shown_on_the_orb_and_never_spoken(wired, monkeypatch):
    """The user sees the brain process swap underneath him ("that's weird why
    did they just randomly restart"). It was once announced with a spoken
    line afterwards; he found that annoying the moment he knew what it was,
    and he was right. So the orb carries a "compacting" state for the
    duration, is put back to idle when it is over, and nothing is said."""
    server = wired
    b = _Brain()
    sp = _Speech()
    frames = []

    async def emit(msg):
        frames.append(msg)

    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "speech", sp)
    monkeypatch.setattr(server, "_voice_emit", emit)

    await server._maybe_rotate()

    assert sp.said == [], f"nothing is spoken about a rotation, but heard: {sp.said}"
    states = [f["state"] for f in frames if f.get("type") == "status"]
    assert states[0] == "compacting", states
    assert states[-1] == "idle", "the orb must be put back when it is over"


@pytest.mark.asyncio
async def test_a_failed_rotation_says_nothing(wired, monkeypatch):
    """rotate() returning False means the old brain is still serving --
    nothing happened from the user's point of view, so nothing is said."""
    server = wired
    b = _Brain(rotates=False)
    sp = _Speech()
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "speech", sp)

    await server._maybe_rotate()

    assert sp.said == []


@pytest.mark.asyncio
async def test_rotation_without_a_speech_scheduler_does_not_raise(wired, monkeypatch):
    """Boot order: rotation logic must tolerate speech being None (as it is
    before start_brain_and_speech has run)."""
    server = wired
    b = _Brain()
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "speech", None)

    await server._maybe_rotate()   # must not raise

    assert b.rotated_with is not None


@pytest.mark.asyncio
async def test_nothing_happens_when_no_rotation_is_pending(wired, monkeypatch):
    server = wired
    b = _Brain(pending=False)
    monkeypatch.setattr(server, "brain_instance", b)

    await server._maybe_rotate()

    assert b.rotated_with is None and b.asked == []


@pytest.mark.asyncio
async def test_a_brain_that_will_not_write_a_journal_still_rotates(wired, monkeypatch):
    """A generation must never vanish without a trace, and a silent brain must
    not block rotation forever."""
    server = wired
    b = _Brain()

    async def refuse(text, origin="user", on_delta=None):
        import brain
        return brain.TurnResult(origin, "", "timeout")

    b.turn = refuse
    monkeypatch.setattr(server, "brain_instance", b)

    await server._maybe_rotate()

    import jarvis_memory as jm
    assert jm.latest_journal(include_placeholders=True) is not None, \
        "the server wrote a minimal entry"
    assert jm.latest_journal() is None, \
        "but it is a tombstone, not something to hand the next generation"
    assert b.rotation_pending is False, "rotation happened anyway"


@pytest.mark.asyncio
async def test_an_error_turn_is_not_persisted_as_a_handover(wired, monkeypatch):
    """A failed turn may still carry the CLI's error text. That is not a
    handover and must not be fed to the next generation as one."""
    server = wired
    b = _Brain()

    async def errored(text, origin="user", on_delta=None):
        import brain
        return brain.TurnResult(origin, "API Error: overloaded_error", "error")

    b.turn = errored
    monkeypatch.setattr(server, "brain_instance", b)

    await server._maybe_rotate()

    assert b.rotated_with is None, "no handover carried forward"
    assert b.rotation_pending is False, "but it still rotated"
    import jarvis_memory as jm
    assert "API Error" not in (jm.latest_journal() or "")


@pytest.mark.asyncio
async def test_rotation_waits_while_another_turn_is_in_flight(wired, monkeypatch):
    """A pause means nothing is being served. Mid-conversation is not a pause."""
    server = wired
    b = _Brain()
    b.current_origin = "user"
    monkeypatch.setattr(server, "brain_instance", b)

    await server._maybe_rotate()

    assert b.asked == [] and b.rotations == 0
    assert b.rotation_pending is True, "still pending, for the next real pause"


@pytest.mark.asyncio
async def test_an_overdue_rotation_happens_even_mid_conversation(wired, monkeypatch):
    """A conversation that never pauses still has to rotate eventually."""
    server = wired
    b = _Brain(overdue=True)
    b.current_origin = "user"
    monkeypatch.setattr(server, "brain_instance", b)

    await server._maybe_rotate()

    assert b.rotation_pending is False


@pytest.mark.asyncio
async def test_two_pauses_at_once_produce_one_rotation(wired, monkeypatch):
    """_handle_utterance runs as a task per utterance, so two can reach the
    pause together. That must not buy two handovers and two process swaps."""
    server = wired
    b = _Brain()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(text, origin="user", on_delta=None):
        b.asked.append((text, origin))
        started.set()
        await release.wait()
        import brain
        return brain.TurnResult(origin, "handover", "result")

    b.turn = slow
    monkeypatch.setattr(server, "brain_instance", b)

    first = asyncio.create_task(server._maybe_rotate())
    # Bounded: if the handover is never asked for at all, this test must fail
    # on its assertions rather than hang the suite.
    try:
        await asyncio.wait_for(started.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        pass
    second = asyncio.create_task(server._maybe_rotate())
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=5.0)

    assert len(b.asked) == 1, "the brain is asked once, not twice"
    assert b.rotations == 1, "the process is swapped once, not twice"


@pytest.mark.asyncio
async def test_a_failed_rotation_does_not_re_ask_at_every_pause(wired, monkeypatch):
    """rotate() returning False leaves rotation_pending True. Retrying is
    right; spending another brain turn and another journal entry on every
    utterance until it succeeds is not."""
    server = wired
    b = _Brain(rotates=False)
    monkeypatch.setattr(server, "brain_instance", b)
    clock = _Clock()
    monkeypatch.setattr(server, "_rotation_clock", clock)

    for _ in range(3):
        await server._maybe_rotate()
        clock.now += server.ROTATION_RETRY_MAX_SEC     # past any backoff

    assert len(b.asked) == 1, "the handover already paid for is reused"
    assert b.rotations == 3, "but rotation is still retried once the backoff passes"


@pytest.mark.asyncio
async def test_a_journal_write_failure_does_not_stop_the_rotation(wired, monkeypatch):
    """Journalling is bookkeeping. A read-only disk must not be able to pin the
    context window open."""
    server = wired
    b = _Brain()
    monkeypatch.setattr(server, "brain_instance", b)

    def boom(text, reason="shutdown"):
        raise OSError("read-only file system")

    monkeypatch.setattr(server.jarvis_memory, "write_journal", boom)

    await server._maybe_rotate()

    assert b.rotation_pending is False


@pytest.mark.asyncio
async def test_shutdown_writes_a_journal(wired, monkeypatch):
    server = wired
    monkeypatch.setattr(server, "brain_instance", _Brain(pending=False))
    monkeypatch.setattr(server, "speech", None)

    await server.stop_brain_and_speech()

    import jarvis_memory as jm
    assert jm.latest_journal() is not None


@pytest.mark.asyncio
async def test_shutdown_journals_even_when_the_brain_never_started(wired, monkeypatch):
    """Autostart off, or a brain that failed to boot: there is still a shutdown
    to record, and the entry is the only trace of it."""
    server = wired
    monkeypatch.setattr(server, "brain_instance", None)
    monkeypatch.setattr(server, "speech", None)

    await server.stop_brain_and_speech()

    import jarvis_memory as jm
    assert jm.latest_journal(include_placeholders=True) is not None
    assert jm.latest_journal() is None, "a tombstone is not a handover"


@pytest.mark.asyncio
async def test_a_journal_failure_cannot_prevent_shutdown(wired, monkeypatch):
    server = wired
    b = _Brain(pending=False)
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "speech", None)

    def boom(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(server.jarvis_memory, "write_journal", boom)

    await server.stop_brain_and_speech()

    assert b.stopped, "the brain was still stopped"
    assert server.brain_instance is None


@pytest.mark.asyncio
async def test_a_wedged_brain_cannot_hold_shutdown_open(wired, monkeypatch):
    """The brain's own turn timeout is 90s. Shutdown does not wait that long."""
    server = wired
    b = _Brain(pending=False)

    async def never(text, origin="user", on_delta=None):
        await asyncio.sleep(3600)

    b.turn = never
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "speech", None)
    monkeypatch.setattr(server, "SHUTDOWN_JOURNAL_TIMEOUT", 0.05)

    await asyncio.wait_for(server.stop_brain_and_speech(), timeout=5.0)

    import jarvis_memory as jm
    assert jm.latest_journal(include_placeholders=True) is not None
    assert jm.latest_journal() is None, "a tombstone is not a handover"
    assert b.stopped


# ---------------------------------------------------------------------------
# Boot: the other end of the handover. A restart must pick up where the last
# generation left off, and be told who is working right now.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_boot_wires_the_brain_to_the_active_projects(wired, monkeypatch):
    server = wired
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")     # never spawn a real claude

    await server.start_brain_and_speech()
    try:
        assert server.brain_instance is not None
        assert server.brain_instance.active_projects is server._active_project_names
    finally:
        await server.stop_brain_and_speech()


def test_active_project_names_is_empty_before_the_watcher_polls(wired, monkeypatch):
    """Boot order puts the brain before the watcher, so this must degrade to
    an empty list rather than raise."""
    server = wired
    monkeypatch.setattr(server, "session_watcher", None)
    assert server._active_project_names() == []


def test_active_project_names_skips_finished_and_never_started_sessions(wired,
                                                                        monkeypatch):
    """`gone` conversations linger in the snapshot for ten minutes so a
    completion can still be announced; a `fresh` window has never been
    prompted. Neither is work in progress."""
    import session_watch
    server = wired

    def state(project, st):
        return session_watch.SessionState(
            session_id=f"{project}-{st}", project=project, cwd="/tmp", state=st)

    class _Watcher:
        snapshot = session_watch.Snapshot(sessions=[
            state("chitauri", session_watch.WORKING),
            state("jarvis", session_watch.NEEDS_YOU),
            state("old-thing", session_watch.GONE),
            state("never-used", session_watch.FRESH),
        ])

    monkeypatch.setattr(server, "session_watcher", _Watcher())

    assert server._active_project_names() == ["chitauri", "jarvis"]


# ── the pause the user sits through ─────────────────────────────────────────
# Collecting a handover and swapping the process takes seconds during which
# nothing answers. The line spoken afterwards explains it too late: by then
# the user has already watched a dead orb and assumed a crash.

@pytest.mark.asyncio
async def test_the_pause_is_announced_before_it_starts(wired, monkeypatch):
    server = wired
    frames = []

    async def _emit(msg):
        frames.append(dict(msg))

    monkeypatch.setattr(server, "brain_instance", _Brain())
    monkeypatch.setattr(server, "speech", _Speech())
    monkeypatch.setattr(server, "_voice_emit", _emit)

    await server._maybe_rotate()

    notices = [f for f in frames if f.get("type") == "notice"]
    assert notices, "the user was given no warning at all"
    assert notices[0]["text"] == server.ROTATION_BUSY_LINE
    assert notices[-1]["text"] == "", "the banner must be cleared afterwards"


@pytest.mark.asyncio
async def test_the_busy_banner_is_shown_and_never_spoken(wired, monkeypatch):
    """The banner is for the screen. Reading it aloud would be the very
    announcement the user asked not to hear."""
    server = wired
    sp = _Speech()
    monkeypatch.setattr(server, "brain_instance", _Brain())
    monkeypatch.setattr(server, "speech", sp)
    monkeypatch.setattr(server, "_voice_emit", lambda msg: asyncio.sleep(0))

    await server._maybe_rotate()

    spoken = [t for t, _p, _i in sp.said]
    assert server.ROTATION_BUSY_LINE not in spoken
    assert spoken == []


@pytest.mark.asyncio
async def test_a_client_that_has_gone_away_cannot_stop_a_rotation(wired, monkeypatch):
    """The notice is a courtesy; the rotation is not optional."""
    server = wired
    b = _Brain()

    async def _boom(msg):
        raise ConnectionError("no voice client connected")

    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "speech", _Speech())
    monkeypatch.setattr(server, "_voice_emit", _boom)

    await server._maybe_rotate()

    assert b.rotations == 1, "the rotation must happen whether or not anyone is listening"


# ---------------------------------------------------------------------------
# The rotation loop of 2026-09-25/26 (see tests/test_rotation_loop.py for the
# measurement half). Every user turn landed on a new brain, and the log said
# "rotation did not happen" after each rotation that DID happen.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_rotation_that_happened_is_not_logged_as_one_that_did_not(
        wired, monkeypatch, caplog):
    """The `else:` that logs the failure sat under the `try` that clears the
    banner, not under `if rotated:` — so it ran whenever clearing the banner
    did not raise, which is always. Five real rotations were each reported
    as a failure, which is what hid a loop that ran for five hours."""
    import logging
    server = wired
    b = _Brain()
    monkeypatch.setattr(server, "brain_instance", b)

    with caplog.at_level(logging.INFO, logger="jarvis"):
        await server._maybe_rotate()

    assert b.rotations == 1 and b.rotation_pending is False
    said = [r.getMessage() for r in caplog.records]
    assert not any("did not happen" in m for m in said), said


@pytest.mark.asyncio
async def test_a_rotation_that_failed_is_logged_and_retried_with_the_same_handover(
        wired, monkeypatch, caplog):
    """The other half, which must keep working: a replacement that will not
    start leaves the old brain serving and the rotation pending, and the next
    pause retries with the handover already paid for."""
    import logging
    server = wired
    b = _Brain(rotates=False)
    monkeypatch.setattr(server, "brain_instance", b)
    clock = _Clock()
    monkeypatch.setattr(server, "_rotation_clock", clock)

    with caplog.at_level(logging.INFO, logger="jarvis"):
        await server._maybe_rotate()
        clock.now += server.ROTATION_RETRY_MAX_SEC
        await server._maybe_rotate()

    assert b.rotations == 2, "a later pause retries"
    assert len(b.asked) == 1, "the handover is asked for once, not at every pause"
    failures = [r for r in caplog.records if "did not happen" in r.getMessage()]
    assert len(failures) == 2 and all(r.levelname == "WARNING" for r in failures)


@pytest.mark.asyncio
async def test_tool_using_turns_do_not_rotate_the_real_brain_at_every_pause(
        wired, monkeypatch, tmp_path):
    """The loop end to end: the real Brain, the stand-in CLI speaking the
    measured stream shape with the live ~99,000-token floor, and the
    server's own pause. Four turns that each call three tools add ~40,000
    tokens of a 120,000 budget. Before the fix, every one of those pauses
    swapped the brain, and the user heard a JARVIS that had forgotten the
    card it staged a minute earlier."""
    import brain
    from tests.test_brain import _config
    server = wired
    monkeypatch.setenv("FAKE_BRAIN_FLOOR", "90000")
    frames = []

    async def emit(msg):
        frames.append(msg)

    b = brain.Brain(_config(tmp_path, context_budget=120_000))
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "_voice_emit", emit)
    try:
        await b.start()
        for i in range(4):
            await b.turn(f"CALLS:4 turn {i}")
            await server._maybe_rotate()
            assert b.generation == 1, (
                f"turn {i} rotated the brain: conversation={b.conversation_tokens} "
                f"floor={b.baseline_tokens}")
        assert not [f for f in frames if f.get("state") == "compacting"], (
            "the orb must not show a rotation that never needed to happen")
    finally:
        await b.stop()


def test_the_handover_request_asks_for_the_note_and_no_tool(wired):
    """Every live handover turn reached for `write_journal` first. The
    request runs as origin="system", so the acting-tool gate refused it:
    one wasted API call per rotation, and a refusal sitting in the very
    context the note was being written from. The server writes the journal
    itself; the request says so."""
    request = wired.JOURNAL_REQUEST.lower()
    assert "do not call any tool" in request
    assert "reply with the note itself" in request


@pytest.mark.asyncio
async def test_a_replacement_that_keeps_failing_is_retried_with_backoff(
        wired, monkeypatch, caplog):
    """Each attempt spawns a whole brain and waits up to its warm-up timeout
    holding the turn lock, with the orb on "compacting". A `claude` that is
    broken for good made every pause that: a turn, then dead air. The first
    retry waits `ROTATION_RETRY_BASE_SEC`, each after that twice as long,
    and a success starts the count again."""
    import logging
    server = wired
    b = _Brain(rotates=False)
    monkeypatch.setattr(server, "brain_instance", b)
    clock = _Clock()
    monkeypatch.setattr(server, "_rotation_clock", clock)
    base = server.ROTATION_RETRY_BASE_SEC

    with caplog.at_level(logging.INFO, logger="jarvis"):
        await server._maybe_rotate()
        assert b.rotations == 1
        await server._maybe_rotate()                   # at once: backing off
        clock.now += base - 1
        await server._maybe_rotate()                   # still inside the first wait
        assert b.rotations == 1
        clock.now += 2
        await server._maybe_rotate()                   # past it: the second attempt
        assert b.rotations == 2
        clock.now += base + 1                          # the second wait is 2 x base
        await server._maybe_rotate()
        assert b.rotations == 2
        clock.now += base
        await server._maybe_rotate()
        assert b.rotations == 3

    said = [r.getMessage() for r in caplog.records if "did not happen" in r.getMessage()]
    assert len(said) == 3 and all("retrying" in m for m in said), said

    b._rotates = True                                  # the CLI is fixed
    clock.now += server.ROTATION_RETRY_MAX_SEC
    await server._maybe_rotate()
    assert b.rotation_pending is False and b.rotations == 4

    # A success starts the count over: the next failure waits the FIRST
    # interval again, not the fourth.
    b._rotates = False
    b.rotation_pending = True                          # a later, real rotation
    await server._maybe_rotate()
    assert b.rotations == 5
    clock.now += base + 1
    await server._maybe_rotate()
    assert b.rotations == 6, "a success starts the backoff over"


@pytest.mark.asyncio
async def test_the_backoff_is_capped(wired, monkeypatch):
    server = wired
    b = _Brain(rotates=False)
    monkeypatch.setattr(server, "brain_instance", b)
    clock = _Clock()
    monkeypatch.setattr(server, "_rotation_clock", clock)
    for _ in range(12):
        await server._maybe_rotate()
        clock.now += server.ROTATION_RETRY_MAX_SEC + 1
    assert b.rotations == 12, "no wait ever grows past the cap"


@pytest.mark.asyncio
async def test_a_handover_from_an_earlier_generation_is_not_reused(wired, monkeypatch):
    """The handover paid for at a pending rotation belongs to the generation
    that wrote it. If that process is replaced some other way first -- it
    died, and was restarted -- a later rotation of its successor must ask the
    successor, or everything said to it is lost."""
    server = wired
    b = _Brain(rotates=False)
    monkeypatch.setattr(server, "brain_instance", b)
    clock = _Clock()
    monkeypatch.setattr(server, "_rotation_clock", clock)

    await server._maybe_rotate()                  # generation 1 writes its note
    assert len(b.asked) == 1

    b.generation = 2                              # crashed and restarted
    b._rotates = True
    clock.now += server.ROTATION_RETRY_MAX_SEC
    await server._maybe_rotate()

    assert len(b.asked) == 2, "generation 2 is asked for its own handover"


@pytest.mark.asyncio
async def test_a_fresh_start_that_did_not_happen_is_not_announced(wired, monkeypatch, caplog):
    """`rotate()` reports a replacement that would not start by RETURNING
    False. The fresh start ignored it and told the user "Cleared" while the
    generation he wanted gone -- the one whose memory writes are refused --
    went on serving."""
    import logging
    server = wired
    b = _Brain(rotates=False)
    sp = _Speech()
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "speech", sp)

    with caplog.at_level(logging.INFO, logger="jarvis"):
        await server._start_fresh()

    spoken = [t for t, _p, _i in sp.said]
    assert server.FRESH_START_LINE not in spoken
    assert spoken and "couldn't clear" in spoken[0]
    said = [r.getMessage() for r in caplog.records]
    assert not any("generation discarded" in m for m in said), said
    assert any("fresh start did not happen" in m for m in said), said


@pytest.mark.asyncio
async def test_a_fresh_start_that_happened_is_announced(wired, monkeypatch):
    server = wired
    b = _Brain()
    sp = _Speech()
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "speech", sp)

    await server._start_fresh()

    assert [t for t, _p, _i in sp.said] == [server.FRESH_START_LINE]
    assert b.rotated_with is None, "a fresh start carries no handover"


def test_the_standing_instructions_do_not_contradict_the_handover_request(wired):
    """JOURNAL_REQUEST says not to call a tool, but the persona and the
    tool's own description both said `write_journal` is how to answer it —
    and that call is refused in the system-origin turn that asks, every
    time. The three must say the same thing."""
    from pathlib import Path
    import jarvis_mcp
    persona = (Path(wired.__file__).parent / "jarvis_home" / "CLAUDE.md").read_text(
        encoding="utf-8")
    spec = next(t for t in jarvis_mcp.TOOL_SPECS if t["name"] == "write_journal")
    for text in (persona, spec["description"]):
        assert "before your context is rotated" not in text
        assert "reply with the note" in text.lower(), text[:200]



@pytest.mark.asyncio
async def test_a_new_generation_from_elsewhere_is_not_held_by_an_old_backoff(
        wired, monkeypatch):
    """The backoff is about ONE replacement that would not start. When the
    brain has become a new generation some other way — a crash restart, or
    a fresh start — whatever failed is behind it, and its first due rotation
    must not wait out the old generation's ten minutes."""
    server = wired
    b = _Brain(rotates=False)
    monkeypatch.setattr(server, "brain_instance", b)
    clock = _Clock()
    monkeypatch.setattr(server, "_rotation_clock", clock)

    for _ in range(4):                                  # the wait is now 4 x base
        await server._maybe_rotate()
        clock.now += server.ROTATION_RETRY_MAX_SEC
    await server._maybe_rotate()
    assert b.rotations == 5

    b.generation = 7                                    # restarted since
    b._rotates = True
    await server._maybe_rotate()                        # at once
    assert b.rotations == 6 and b.rotation_pending is False


@pytest.mark.asyncio
async def test_a_fresh_start_that_worked_clears_the_backoff(wired, monkeypatch):
    server = wired
    b = _Brain(rotates=False)
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "speech", _Speech())
    clock = _Clock()
    monkeypatch.setattr(server, "_rotation_clock", clock)

    await server._maybe_rotate()                        # fails: backing off
    b._rotates = True
    await server._start_fresh()                         # the user's word works
    assert server._rotation_failures == 0

    b.rotation_pending = True
    await server._maybe_rotate()
    assert b.rotations == 3, "no leftover wait after a fresh start"
