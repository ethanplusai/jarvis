"""Tests for answer_dialog — the one feature that types on the user's machine.

NOT ONE TEST IN THIS FILE MAY SEND A REAL KEYSTROKE OR ACTIVATE AN APPLICATION.
The developer runs this suite with sixteen live Claude Code sessions and real
work open; a stray Return would land in whichever of them happened to be
frontmost. Every test therefore mocks `dialog._osascript` — the single process
boundary — and `dialog._terminal_is_running`, so `osascript` is never spawned
and Terminal.app is never even asked whether it exists. If you add a test here
that does not stub both, it is wrong however green it runs.

On Windows the boundary is `dialog._helper` (the console helper process).
The autouse fixture below pins the macOS path for this file and makes that
helper fail any test that reaches it; the Windows path is tested, with the
helper stubbed, in test_dialog_windows.py.
"""

import asyncio
import time

import sys

import pytest

import dialog


@pytest.fixture(autouse=True)
def _the_macos_path_and_no_console_helper(monkeypatch):
    monkeypatch.setattr(dialog, "_WINDOWS", False)

    def _never(*args, **kwargs):
        raise AssertionError(f"a test reached the real console helper: {args}")

    monkeypatch.setattr(dialog, "_helper", _never)


# --- the closed vocabulary --------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("return", "return"), ("Return", "return"), ("  enter ", "return"),
    ("yes", "return"), ("y", "return"), ("enter\n", "return"),
    ("escape", "escape"), ("ESC", "escape"), ("cancel", "escape"),
    ("no", "escape"), ("n", "escape"),
    ("1", "1"), ("9", "9"), ("5", "5"),
])
def test_the_accepted_vocabulary_normalizes(raw, expected):
    assert dialog.normalize_key(raw) == expected


@pytest.mark.parametrize("raw", [
    "rm -rf /",                       # free text
    "",                               # empty
    "   ",                            # whitespace only
    "yes please",                     # multi-character, starts with an alias
    "returnn",
    "0",                              # a digit outside 1-9
    "10",                             # two digits
    "-1",
    '"; do shell script "',           # injection-shaped
    "return; keystroke \"rm\"",
    "\n",
    "key code 36",
    None,
    36,
    ["return"],
])
def test_everything_outside_the_vocabulary_is_refused(raw):
    assert dialog.normalize_key(raw) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    "rm -rf /", "", "   ", "yes please", "0", "10", '"; do shell script "',
    "return; keystroke \"rm\"", None, 36,
])
async def test_a_refused_key_never_reaches_applescript(raw, monkeypatch):
    """The refusal happens before a script exists, so there is nothing to
    escape and nothing to get wrong."""
    calls = []

    async def never(script, timeout):
        calls.append(script)
        raise AssertionError("osascript must not run for a refused key")

    monkeypatch.setattr(dialog, "_osascript", never)
    monkeypatch.setattr(dialog, "_terminal_is_running", lambda: True)
    monkeypatch.setattr(dialog, "tty_for_pid",
                        lambda pid: pytest.fail("the key is checked first"))

    assert await dialog.answer(4242, raw) == dialog.BAD_KEY
    assert calls == []


# --- the tty -> tab mapping -------------------------------------------------

def test_a_tty_is_normalized_to_its_device_path():
    assert dialog.normalize_tty("ttys006") == "/dev/ttys006"
    assert dialog.normalize_tty("/dev/ttys006") == "/dev/ttys006"
    assert dialog.normalize_tty("??") is None
    assert dialog.normalize_tty("") is None
    assert dialog.normalize_tty(None) is None
    assert dialog.normalize_tty("/dev/ttys006; rm -rf /") is None


def test_a_dead_pid_has_no_tty():
    # ps exits non-zero for a pid that does not exist. 2**31 - 1 is above
    # macOS's pid ceiling, so it cannot be a live process on this machine.
    assert dialog.tty_for_pid(2 ** 31 - 1) is None
    assert dialog.tty_for_pid(0) is None
    assert dialog.tty_for_pid(-1) is None
    assert dialog.tty_for_pid("nonsense") is None


def _enumeration(*rows):
    return "".join(f"{wid}:{idx}:{tty}\n" for wid, idx, tty in rows)


@pytest.fixture
def fake_osascript(monkeypatch):
    """Records every script and replays queued (rc, stdout, stderr) results."""
    calls = []
    results = []

    async def run(script, timeout):
        calls.append(script)
        return results.pop(0) if results else (0, "", "")

    monkeypatch.setattr(dialog, "_osascript", run)
    monkeypatch.setattr(dialog, "_terminal_is_running", lambda: True)
    return calls, results


@pytest.mark.asyncio
async def test_find_terminal_tab_matches_the_tty_exactly(fake_osascript):
    calls, results = fake_osascript
    results.append((0, _enumeration((11, 1, "/dev/ttys001"),
                                    (11, 2, "/dev/ttys011"),
                                    (12, 1, "/dev/ttys006")), ""))

    tab = await dialog.find_terminal_tab("ttys011")

    assert tab == dialog.TerminalTab(window_id=11, tab_index=2, tty="/dev/ttys011")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_tty_is_never_matched_by_prefix(fake_osascript):
    """ttys1 must not match ttys11: a suffix or substring test here would
    aim the keystroke at a neighbouring tab."""
    calls, results = fake_osascript
    results.append((0, _enumeration((11, 1, "/dev/ttys11")), ""))
    assert await dialog.find_terminal_tab("ttys1") is None


@pytest.mark.asyncio
async def test_terminal_is_never_launched_just_to_look(monkeypatch):
    """`tell application "Terminal"` would OPEN Terminal.app. A read-only
    lookup must not put a window on the user's screen."""
    async def never(script, timeout):
        raise AssertionError("Terminal must not be addressed when it is not running")

    monkeypatch.setattr(dialog, "_osascript", never)
    monkeypatch.setattr(dialog, "_terminal_is_running", lambda: False)

    assert await dialog.find_terminal_tab("/dev/ttys006") is None


# --- every outcome of answer() ----------------------------------------------

@pytest.mark.asyncio
async def test_sent(fake_osascript, monkeypatch):
    calls, results = fake_osascript
    monkeypatch.setattr(dialog, "tty_for_pid", lambda pid: "/dev/ttys006")
    results.append((0, _enumeration((12, 3, "/dev/ttys006")), ""))
    results.append((0, "ok\n", ""))

    assert await dialog.answer(999, "yes") == dialog.SENT

    assert len(calls) == 2
    assert "key code 36" in calls[1]


@pytest.mark.asyncio
async def test_no_tty_presses_nothing(fake_osascript, monkeypatch):
    calls, _ = fake_osascript
    monkeypatch.setattr(dialog, "tty_for_pid", lambda pid: None)

    assert await dialog.answer(999, "return") == dialog.NO_TTY
    assert calls == [], "a session with no terminal must not reach AppleScript"


@pytest.mark.asyncio
async def test_not_found_when_no_terminal_tab_owns_that_tty(fake_osascript,
                                                            monkeypatch):
    """The common case on this machine: the session is hosted by Orcha.app,
    whose tabs AppleScript cannot address. Nothing may be pressed."""
    calls, results = fake_osascript
    monkeypatch.setattr(dialog, "tty_for_pid", lambda pid: "/dev/ttys042")
    results.append((0, _enumeration((11, 1, "/dev/ttys001"),
                                    (11, 2, "/dev/ttys002")), ""))

    assert await dialog.answer(999, "return") == dialog.NOT_FOUND

    assert len(calls) == 1, "only the read-only enumeration may have run"
    assert "keystroke" not in calls[0] and "key code" not in calls[0]
    assert "activate" not in calls[0]


@pytest.mark.asyncio
async def test_not_found_when_terminal_is_not_running_at_all(fake_osascript,
                                                             monkeypatch):
    calls, _ = fake_osascript
    monkeypatch.setattr(dialog, "tty_for_pid", lambda pid: "/dev/ttys006")
    monkeypatch.setattr(dialog, "_terminal_is_running", lambda: False)

    assert await dialog.answer(999, "return") == dialog.NOT_FOUND
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stderr", [
    "execution error: osascript is not allowed assistive access. (-25211)",
    "execution error: Not authorized to send Apple events to System Events. (-1743)",
    "System Events got an error: osascript is not allowed to send keystrokes.",
])
async def test_not_permitted(stderr, fake_osascript, monkeypatch):
    _, results = fake_osascript
    monkeypatch.setattr(dialog, "tty_for_pid", lambda pid: "/dev/ttys006")
    results.append((0, _enumeration((12, 1, "/dev/ttys006")), ""))
    results.append((1, "", stderr))

    assert await dialog.answer(999, "escape") == dialog.NOT_PERMITTED


@pytest.mark.asyncio
async def test_failed_when_the_script_errors_for_another_reason(fake_osascript,
                                                                monkeypatch):
    _, results = fake_osascript
    monkeypatch.setattr(dialog, "tty_for_pid", lambda pid: "/dev/ttys006")
    results.append((0, _enumeration((12, 1, "/dev/ttys006")), ""))
    results.append((1, "", "execution error: something else entirely (-1728)"))

    assert await dialog.answer(999, "3") == dialog.FAILED


@pytest.mark.asyncio
async def test_failed_when_osascript_hangs_and_is_killed(fake_osascript,
                                                         monkeypatch):
    """A hung osascript is time-boxed rather than allowed to wedge the voice
    turn waiting on it."""
    _, results = fake_osascript
    monkeypatch.setattr(dialog, "tty_for_pid", lambda pid: "/dev/ttys006")
    results.append((0, _enumeration((12, 1, "/dev/ttys006")), ""))
    results.append((-1, "", "timeout"))

    assert await dialog.answer(999, "return") == dialog.FAILED


@pytest.mark.asyncio
async def test_answer_never_raises(fake_osascript, monkeypatch):
    def boom(pid):
        raise RuntimeError("ps exploded")

    monkeypatch.setattr(dialog, "tty_for_pid", boom)
    assert await dialog.answer(999, "return") == dialog.FAILED


@pytest.mark.asyncio
async def test_a_tab_that_moved_between_lookup_and_press_is_not_pressed(
        fake_osascript, monkeypatch):
    _, results = fake_osascript
    monkeypatch.setattr(dialog, "tty_for_pid", lambda pid: "/dev/ttys006")
    results.append((0, _enumeration((12, 1, "/dev/ttys006")), ""))
    results.append((0, "moved\n", ""))

    assert await dialog.answer(999, "return") == dialog.NOT_FOUND


# --- focus ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_frontmost_app_is_captured_first_and_restored_after(
        fake_osascript, monkeypatch):
    _, results = fake_osascript
    monkeypatch.setattr(dialog, "tty_for_pid", lambda pid: "/dev/ttys006")
    results.append((0, _enumeration((12, 1, "/dev/ttys006")), ""))
    results.append((0, "ok\n", ""))
    calls, _ = fake_osascript

    assert await dialog.answer(999, "return") == dialog.SENT

    send = calls[1]
    capture = send.index("name of first application process whose frontmost is true")
    activate = send.index("activate")
    press = send.index("key code 36")
    restore = send.index("set frontmost of process priorApp to true")
    assert capture < activate < press < restore, (
        "the previous app must be recorded before Terminal is raised and "
        "restored after the keystroke")


@pytest.mark.asyncio
async def test_the_send_script_re_checks_the_tty_before_pressing(fake_osascript,
                                                                 monkeypatch):
    _, results = fake_osascript
    monkeypatch.setattr(dialog, "tty_for_pid", lambda pid: "/dev/ttys006")
    results.append((0, _enumeration((12, 1, "/dev/ttys006")), ""))
    results.append((0, "ok\n", ""))
    calls, _ = fake_osascript

    await dialog.answer(999, "return")

    send = calls[1]
    assert 'if nowTty is not "/dev/ttys006" then return "moved"' in send
    assert send.index("nowTty is not") < send.index("key code 36")


@pytest.mark.asyncio
async def test_a_digit_is_sent_as_that_one_character_and_nothing_else(
        fake_osascript, monkeypatch):
    _, results = fake_osascript
    monkeypatch.setattr(dialog, "tty_for_pid", lambda pid: "/dev/ttys006")
    results.append((0, _enumeration((12, 1, "/dev/ttys006")), ""))
    results.append((0, "ok\n", ""))
    calls, _ = fake_osascript

    await dialog.answer(999, "2")

    assert 'keystroke "2"' in calls[1]
    assert calls[1].count("keystroke") == 1


# --- the tool, and the staged pattern ---------------------------------------

@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    import importlib
    import server as server_module
    importlib.reload(server_module)
    server_module.run_store.init_db()
    # Belt and braces: even reloaded, the server module must never be able to
    # reach a real osascript from this file.
    async def never(script, timeout):
        raise AssertionError("no test may run osascript")
    monkeypatch.setattr(dialog, "_osascript", never)
    monkeypatch.setattr(dialog, "_terminal_is_running", lambda: False)
    # These tests drive the macOS flow with the keypress itself faked, so
    # they hold on any platform. The Windows refusal has its own test below.
    monkeypatch.setattr(server_module, "_keypress_supported", lambda: True)
    return server_module


class FakeUtterance:
    def __init__(self, cancelled=False):
        self.was_cancelled = cancelled


class FakeSpeech:
    """Models say -> wait_for -> open_cancel_window, and logs the order of
    everything so a test can prove nothing was pressed too early."""
    def __init__(self, log=None, cancelled=False, readback_cancelled=False,
                 readback_heard=True):
        self.said = []
        self.log = log if log is not None else []
        self.cancelled = cancelled
        self.readback_cancelled = readback_cancelled
        self.readback_heard = readback_heard

    async def say(self, text, *a, **k):
        self.said.append(text)
        self.log.append(("said", text))
        return FakeUtterance(cancelled=self.readback_cancelled)

    async def wait_for(self, utt, timeout=60.0):
        self.log.append(("waited", None))
        if not self.readback_heard:
            return False
        return not utt.was_cancelled

    async def open_cancel_window(self, *a, **k):
        self.log.append(("window", None))
        return self.cancelled


class FakeBrain:
    current_origin = "user"


def _state(pids=(4242,), voice_name="hammer", project="hammer"):
    import session_watch as sw
    return sw.SessionState(session_id="sid", cwd="/p/hammer", project=project,
                           state="needs_you", voice_name=voice_name,
                           pids=list(pids), primary_pid=pids[0])


@pytest.fixture
def tool(wired, monkeypatch):
    """server + a recorded fake keypress, with a session that resolves and
    has exactly one tty."""
    server = wired
    log = []
    speech = FakeSpeech(log=log)
    pressed = []

    async def fake_answer(pid, key):
        log.append(("pressed", (pid, key)))
        pressed.append((pid, key))
        return dialog.SENT

    monkeypatch.setattr(server, "speech", speech)
    monkeypatch.setattr(server, "brain_instance", FakeBrain())
    monkeypatch.setattr(server, "_resolve_or_explain",
                        lambda n: (_state(), None, None))
    monkeypatch.setattr(dialog, "tty_for_pid", lambda pid: "/dev/ttys006")
    monkeypatch.setattr(dialog, "answer", fake_answer)
    return server, speech, pressed, log


@pytest.mark.asyncio
async def test_the_tool_stages_and_presses_nothing_at_all(tool):
    """The tool runs mid-turn with the turn utterance still open. Anything it
    said would be queued behind the very turn waiting for it to return."""
    server, speech, pressed, _ = tool

    result = await server.tool_answer_dialog({"name": "hammer", "key": "yes"})

    assert speech.said == [], "the read-back may not happen inside the tool call"
    assert pressed == [], "nothing may be pressed from inside the tool call"
    assert "staged" in result.lower()
    assert len(server._staged_dialogs) == 1
    assert server._staged_dialogs[0].key == "return"
    import run_store
    assert run_store.list_steers(limit=50) == [], "staging is not an outcome"


@pytest.mark.asyncio
async def test_nothing_is_pressed_before_the_readback_has_been_heard(tool):
    server, speech, pressed, log = tool

    await server.tool_answer_dialog({"name": "hammer", "key": "yes"})
    await server._perform_staged_dialogs()

    kinds = [k for k, _ in log]
    assert kinds.index("pressed") > kinds.index("said")
    assert kinds.index("pressed") > kinds.index("waited")
    assert kinds.index("pressed") > kinds.index("window")
    assert pressed == [(4242, "return")]
    assert server._staged_dialogs == []


@pytest.mark.asyncio
async def test_the_readback_names_the_key_and_warns_about_the_focus(tool, monkeypatch):
    server, speech, _, _ = tool
    monkeypatch.setattr(server, "_KEYS_GO_TO_A_CONSOLE", False)

    await server.tool_answer_dialog({"name": "hammer", "key": "yes"})
    await server._perform_staged_dialogs()

    assert "Return" in speech.said[0] and "hammer" in speech.said[0]
    assert "forward" in speech.said[0].lower(), \
        "the user must be warned that this steals focus"


@pytest.mark.asyncio
async def test_on_windows_the_readback_does_not_warn_of_a_window_coming_forward(
        tool, monkeypatch):
    """The key goes into the console's input there; no window moves, so
    warning that one will would be false."""
    server, speech, _, _ = tool
    monkeypatch.setattr(server, "_KEYS_GO_TO_A_CONSOLE", True)

    await server.tool_answer_dialog({"name": "hammer", "key": "yes"})
    await server._perform_staged_dialogs()

    assert "Return" in speech.said[0] and "hammer" in speech.said[0]
    assert "forward" not in speech.said[0].lower()


@pytest.mark.asyncio
async def test_a_cancel_word_in_the_window_blocks_the_keypress(wired, monkeypatch):
    server = wired
    speech = FakeSpeech(cancelled=True)
    pressed = []

    async def fake_answer(pid, key):
        pressed.append((pid, key))
        return dialog.SENT

    monkeypatch.setattr(server, "speech", speech)
    monkeypatch.setattr(server, "brain_instance", FakeBrain())
    monkeypatch.setattr(server, "_resolve_or_explain", lambda n: (_state(), None, None))
    monkeypatch.setattr(dialog, "tty_for_pid", lambda pid: "/dev/ttys006")
    monkeypatch.setattr(dialog, "answer", fake_answer)

    await server.tool_answer_dialog({"name": "hammer", "key": "yes"})
    await server._perform_staged_dialogs()

    assert pressed == [], "nothing may be pressed after a cancel"
    import run_store
    assert run_store.list_steers(limit=5)[0]["outcome"] == "dialog:cancelled_by_user"


@pytest.mark.asyncio
async def test_a_bargein_during_the_readback_blocks_it_and_records_a_cancel(
        wired, monkeypatch):
    """`wait_for` returns False for a cancel and a timeout alike, so
    `was_cancelled` must be checked explicitly and separately."""
    server = wired
    speech = FakeSpeech(readback_cancelled=True)
    pressed = []

    async def fake_answer(pid, key):
        pressed.append((pid, key))
        return dialog.SENT

    monkeypatch.setattr(server, "speech", speech)
    monkeypatch.setattr(server, "brain_instance", FakeBrain())
    monkeypatch.setattr(server, "_resolve_or_explain", lambda n: (_state(), None, None))
    monkeypatch.setattr(dialog, "tty_for_pid", lambda pid: "/dev/ttys006")
    monkeypatch.setattr(dialog, "answer", fake_answer)

    await server.tool_answer_dialog({"name": "hammer", "key": "escape"})
    await server._perform_staged_dialogs()

    assert pressed == []
    import run_store
    assert run_store.list_steers(limit=5)[0]["outcome"] == "dialog:cancelled_by_user"


@pytest.mark.asyncio
async def test_a_readback_that_was_never_heard_presses_nothing(wired, monkeypatch):
    server = wired
    speech = FakeSpeech(readback_heard=False)
    pressed = []

    async def fake_answer(pid, key):
        pressed.append((pid, key))
        return dialog.SENT

    monkeypatch.setattr(server, "speech", speech)
    monkeypatch.setattr(server, "brain_instance", FakeBrain())
    monkeypatch.setattr(server, "_resolve_or_explain", lambda n: (_state(), None, None))
    monkeypatch.setattr(dialog, "tty_for_pid", lambda pid: "/dev/ttys006")
    monkeypatch.setattr(dialog, "answer", fake_answer)

    await server.tool_answer_dialog({"name": "hammer", "key": "yes"})
    await server._perform_staged_dialogs()

    assert pressed == [], "never press something the user cannot be shown to have heard"
    import run_store
    assert run_store.list_steers(limit=5)[0]["outcome"] == "dialog:readback_failed"


@pytest.mark.asyncio
async def test_no_voice_refuses_rather_than_pressing_unannounced(wired, monkeypatch):
    server = wired
    pressed = []

    async def fake_answer(pid, key):
        pressed.append((pid, key))
        return dialog.SENT

    monkeypatch.setattr(server, "speech", None)
    monkeypatch.setattr(server, "brain_instance", FakeBrain())
    monkeypatch.setattr(server, "_resolve_or_explain", lambda n: (_state(), None, None))
    monkeypatch.setattr(dialog, "tty_for_pid", lambda pid: "/dev/ttys006")
    monkeypatch.setattr(dialog, "answer", fake_answer)

    result = await server.tool_answer_dialog({"name": "hammer", "key": "yes"})

    assert pressed == []
    assert server._staged_dialogs == []
    assert "won't press" in result.lower()
    import run_store
    assert run_store.list_steers(limit=5)[0]["outcome"] == "dialog:no_voice"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "rm -rf /", "", "yes please", "0", "10", '"; do shell script "', "returnn",
])
async def test_the_tool_refuses_a_key_outside_the_vocabulary(bad, wired,
                                                             monkeypatch):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)
    monkeypatch.setattr(server, "brain_instance", FakeBrain())
    monkeypatch.setattr(server, "_resolve_or_explain", lambda n: (_state(), None, None))
    monkeypatch.setattr(dialog, "answer",
                        lambda *a: pytest.fail("a refused key must not be pressed"))

    result = await server.tool_answer_dialog({"name": "hammer", "key": bad})

    assert server._staged_dialogs == [], "a refused key may not be staged"
    assert "return" in result.lower() and "escape" in result.lower()
    import run_store
    assert run_store.list_steers(limit=5)[0]["outcome"] == "dialog:bad_key"


@pytest.mark.asyncio
async def test_an_ambiguous_name_asks_rather_than_guessing(wired, monkeypatch):
    server = wired
    monkeypatch.setattr(server, "speech", FakeSpeech())
    monkeypatch.setattr(server, "brain_instance", FakeBrain())
    monkeypatch.setattr(server, "_resolve_or_explain",
                        lambda n: (None, "There are 2: a and b. Which one?", "ambiguous"))
    monkeypatch.setattr(dialog, "answer",
                        lambda *a: pytest.fail("an ambiguous target is never pressed"))

    result = await server.tool_answer_dialog({"name": "hammer", "key": "yes"})

    assert "which one" in result.lower()
    assert server._staged_dialogs == []
    import run_store
    assert run_store.list_steers(limit=5)[0]["outcome"] == "dialog:ambiguous"


@pytest.mark.asyncio
async def test_a_session_with_no_tty_is_refused_synchronously(wired, monkeypatch):
    """The `sdk-cli` entrypoint on this machine reports `??` for its tty."""
    server = wired
    monkeypatch.setattr(server, "speech", FakeSpeech())
    monkeypatch.setattr(server, "brain_instance", FakeBrain())
    monkeypatch.setattr(server, "_resolve_or_explain", lambda n: (_state(), None, None))
    monkeypatch.setattr(dialog, "tty_for_pid", lambda pid: None)
    monkeypatch.setattr(dialog, "answer",
                        lambda *a: pytest.fail("nothing to press"))

    result = await server.tool_answer_dialog({"name": "hammer", "key": "yes"})

    assert "terminal" in result.lower()
    assert server._staged_dialogs == []
    import run_store
    assert run_store.list_steers(limit=5)[0]["outcome"] == "dialog:no_tty"


@pytest.mark.asyncio
async def test_a_session_spanning_two_terminals_asks_rather_than_picking(
        wired, monkeypatch):
    server = wired
    monkeypatch.setattr(server, "speech", FakeSpeech())
    monkeypatch.setattr(server, "brain_instance", FakeBrain())
    monkeypatch.setattr(server, "_resolve_or_explain",
                        lambda n: (_state(pids=(1, 2)), None, None))
    monkeypatch.setattr(dialog, "tty_for_pid",
                        lambda pid: f"/dev/ttys00{pid}")
    monkeypatch.setattr(dialog, "answer",
                        lambda *a: pytest.fail("an ambiguous window is never typed into"))

    result = await server.tool_answer_dialog({"name": "hammer", "key": "yes"})

    assert "won't guess" in result.lower()
    assert server._staged_dialogs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,expected", [
    (dialog.SENT, "dialog:sent"),
    (dialog.NO_TTY, "dialog:no_tty"),
    (dialog.NOT_FOUND, "dialog:not_found"),
    (dialog.NOT_PERMITTED, "dialog:not_permitted"),
    (dialog.FAILED, "dialog:failed"),
])
async def test_every_terminal_path_writes_exactly_one_audit_row(
        outcome, expected, wired, monkeypatch):
    server = wired
    speech = FakeSpeech()

    async def fake_answer(pid, key):
        return outcome

    monkeypatch.setattr(server, "speech", speech)
    monkeypatch.setattr(server, "brain_instance", FakeBrain())
    monkeypatch.setattr(server, "_resolve_or_explain", lambda n: (_state(), None, None))
    monkeypatch.setattr(dialog, "tty_for_pid", lambda pid: "/dev/ttys006")
    monkeypatch.setattr(dialog, "answer", fake_answer)

    await server.tool_answer_dialog({"name": "hammer", "key": "yes"})
    await server._perform_staged_dialogs()

    import run_store
    rows = run_store.list_steers(limit=50)
    assert len(rows) == 1, "exactly one row per attempt, never zero and never two"
    assert rows[0]["outcome"] == expected
    assert rows[0]["prompt"] == "return", "the key is what was audited"
    assert speech.said[-1] != speech.said[0], "the outcome is spoken too"


@pytest.mark.asyncio
async def test_a_raised_exception_still_leaves_an_audit_row(wired, monkeypatch):
    server = wired

    async def boom(pid, key):
        raise RuntimeError("nope")

    monkeypatch.setattr(server, "speech", FakeSpeech())
    monkeypatch.setattr(server, "brain_instance", FakeBrain())
    monkeypatch.setattr(server, "_resolve_or_explain", lambda n: (_state(), None, None))
    monkeypatch.setattr(dialog, "tty_for_pid", lambda pid: "/dev/ttys006")
    monkeypatch.setattr(dialog, "answer", boom)

    await server.tool_answer_dialog({"name": "hammer", "key": "yes"})
    await server._perform_staged_dialogs()      # must not raise out

    import run_store
    rows = run_store.list_steers(limit=50)
    assert rows and rows[0]["outcome"] == "dialog:failed"
    assert server._staged_dialogs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("console, where", [(False, "terminal"), (True, "console")])
async def test_an_unreachable_host_is_reported_honestly(wired, monkeypatch, console, where):
    """The Orcha.app case: no Terminal tab owns that tty (on Windows: the
    session has no console of its own). JARVIS says so rather than claiming
    success or silently doing nothing."""
    server = wired
    monkeypatch.setattr(server, "_KEYS_GO_TO_A_CONSOLE", console)
    speech = FakeSpeech()

    async def fake_answer(pid, key):
        return dialog.NOT_FOUND

    monkeypatch.setattr(server, "speech", speech)
    monkeypatch.setattr(server, "brain_instance", FakeBrain())
    monkeypatch.setattr(server, "_resolve_or_explain", lambda n: (_state(), None, None))
    monkeypatch.setattr(dialog, "tty_for_pid", lambda pid: "/dev/ttys006")
    monkeypatch.setattr(dialog, "answer", fake_answer)

    await server.tool_answer_dialog({"name": "hammer", "key": "yes"})
    await server._perform_staged_dialogs()

    last = speech.said[-1].lower()
    assert where in last and "hammer" in last
    assert "pressed" not in last, "it must not claim a keystroke it did not send"


# --- the registries ---------------------------------------------------------

def test_answer_dialog_is_gated_as_an_acting_tool(wired):
    server = wired
    assert "answer_dialog" in server.TOOL_HANDLERS
    assert "answer_dialog" in server.ACTING_TOOLS, (
        "a synthetic keystroke must never be reachable from another session's "
        "transcript — only from the user's own turn")
    assert server.ACTING_TOOLS <= set(server.TOOL_HANDLERS)


def test_the_three_registries_agree(wired):
    import brain
    import jarvis_mcp
    server = wired
    assert "mcp__jarvis__answer_dialog" in brain.ALLOWED_TOOLS
    assert {t["name"] for t in jarvis_mcp.TOOL_SPECS} == set(server.TOOL_HANDLERS)
    assert {t for t in brain.ALLOWED_TOOLS if t.startswith("mcp__")} <= {
        f"mcp__jarvis__{n}" for n in server.TOOL_HANDLERS}


def test_the_staged_dialogs_are_drained_after_the_turn(wired):
    """The whole point of the staged shape: `_handle_utterance` performs them
    after `end_turn`, never inside the tool call."""
    import inspect
    server = wired
    src = inspect.getsource(server._handle_utterance)
    assert "_perform_staged_dialogs()" in src
    assert "_staged_dialogs" in src


@pytest.mark.asyncio
async def test_where_keypresses_are_unsupported_nothing_is_pressed(wired, monkeypatch):
    """The gate for a platform where neither Terminal.app nor a console can
    be found by identity. Refused, naming the session."""
    server = wired
    monkeypatch.setattr(server, "_keypress_supported", lambda: False)
    monkeypatch.setattr(server, "speech", FakeSpeech())
    monkeypatch.setattr(server, "brain_instance", FakeBrain())
    monkeypatch.setattr(server, "_resolve_or_explain", lambda n: (_state(), None, None))
    monkeypatch.setattr(dialog, "answer", lambda *a: pytest.fail("pressed a key"))

    result = await server.tool_answer_dialog({"name": "hammer", "key": "yes"})

    assert "can't press keys" in result and "hammer" in result
    assert server._staged_dialogs == []
    rows = server.run_store.list_steers(limit=5)
    assert rows and rows[0]["outcome"] == "dialog:unsupported_platform"


def test_on_windows_it_is_offered_and_described_as_windows_does_it(monkeypatch):
    """Offered and granted on Windows since the console port, and the brain
    is told the truth there: a console, not Terminal.app, and nothing comes
    to the front."""
    import brain
    import jarvis_mcp
    monkeypatch.setattr(jarvis_mcp.sys, "platform", "win32")
    monkeypatch.setattr(brain, "_WINDOWS", True)
    spec = next(t for t in jarvis_mcp.offered_tool_specs() if t["name"] == "answer_dialog")
    assert "console window" in spec["description"]
    assert "Terminal.app" not in spec["description"]
    assert "TO THE FRONT" not in spec["description"]
    assert "mcp__jarvis__answer_dialog" in brain.granted_tools([])
    # TOOL_SPECS itself, the macOS wording, is untouched.
    mac = next(t for t in jarvis_mcp.TOOL_SPECS if t["name"] == "answer_dialog")
    assert "Terminal.app" in mac["description"]


# --- neither `ps` nor `pgrep` may run ON the event loop --------------------
#
# `tty_for_pid` uses a blocking `subprocess.run(["ps", …], timeout=5.0)` and
# `_terminal_is_running` a blocking `pgrep`. Both were called straight from
# `answer()`, which is an async function on the voice loop: a slow or hung
# `ps` froze the microphone for up to five seconds per pid.
#
# These measure the only thing that matters — whether the loop keeps turning
# while the lookup is in flight.


# A frozen loop gets ~0 turns in 0.4s. A free one gets a turn per sleep —
# 5ms on macOS, but a whole 15.6ms clock tick on Windows (about 25 turns).
_UNFROZEN_TICKS = 10 if sys.platform == "win32" else 30


class _Ticker:
    def __init__(self):
        self.ticks = 0
        self._task = None

    async def __aenter__(self):
        async def tick():
            while True:
                self.ticks += 1
                await asyncio.sleep(0.005)
        self._task = asyncio.create_task(tick())
        await asyncio.sleep(0.05)
        self.ticks = 0
        return self

    async def __aexit__(self, *exc):
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_a_slow_ps_does_not_freeze_the_loop(monkeypatch):
    def slow(pid):
        time.sleep(0.4)
        return None

    monkeypatch.setattr(dialog, "tty_for_pid", slow)
    async with _Ticker() as ticker:
        assert await dialog.answer(4242, "return") == dialog.NO_TTY
    assert ticker.ticks > _UNFROZEN_TICKS, (
        f"the loop only got {ticker.ticks} turns while `ps` ran — the voice "
        "path shares this thread")


@pytest.mark.asyncio
async def test_a_slow_pgrep_does_not_freeze_the_loop(monkeypatch):
    monkeypatch.setattr(dialog, "tty_for_pid", lambda pid: "/dev/ttys006")

    def slow():
        time.sleep(0.4)
        return False

    monkeypatch.setattr(dialog, "_terminal_is_running", slow)
    async with _Ticker() as ticker:
        assert await dialog.answer(4242, "return") == dialog.NOT_FOUND
    assert ticker.ticks > _UNFROZEN_TICKS, (
        f"the loop only got {ticker.ticks} turns while `pgrep` ran")


def test_the_ps_timeout_bounds_a_synchronous_caller(monkeypatch):
    """server.py's `_tty_for_session_or_explain` is synchronous and calls
    `tty_for_pid` once per pid. Until it can be made async, the per-call
    ceiling is what bounds the damage: a five-process session at 5s each was
    up to 25 seconds of frozen microphone."""
    assert dialog._PS_TIMEOUT <= 2.0
