"""answer_dialog on Windows: one key into a session's own console.

The same rule as test_answer_dialog.py, and it is absolute: NOTHING here may
press a key in a real session. `dialog._helper` is the one process boundary
on Windows, and every test of `dialog` stubs it. The helper itself
(`dialog_windows.py`) is exercised only against consoles these tests start
for the purpose and own: a child that reads three keys and writes down what
it got. Those are marked `browser` (deselected by default) because they
open a console window on the desk.
"""

import asyncio
import os
import subprocess
import sys
import time

import pytest

if sys.platform != "win32":
    pytest.skip("consoles are Windows'", allow_module_level=True)

import dialog
import dialog_windows


@pytest.fixture
def helper(monkeypatch):
    """A stand-in for the console helper process: records every call and
    answers from `replies`, keyed by the subcommand."""
    calls = []
    replies = {"identify": "console:0x1a2b", "press": "ok"}

    def fake(*args, timeout=None):
        calls.append(args)
        return replies[args[0]]

    monkeypatch.setattr(dialog, "_WINDOWS", True)
    monkeypatch.setattr(dialog, "_helper", fake)
    fake.calls, fake.replies = calls, replies
    return fake


# --- identity: the console stands in for the tty -----------------------------

def test_a_pid_with_a_console_is_named_by_it(helper):
    assert dialog.tty_for_pid(4242) == "console:0x1a2b"
    assert helper.calls == [("identify", "4242")]


@pytest.mark.parametrize("answer", ["none", "", "console:", "console:0xZZ",
                                    "/dev/ttys006", "console:0x1a2b; rm -rf"])
def test_anything_but_a_console_name_is_no_console(helper, answer):
    helper.replies["identify"] = answer
    assert dialog.tty_for_pid(4242) is None


def test_a_dead_or_impossible_pid_never_reaches_the_helper(helper):
    assert dialog.tty_for_pid(0) is None
    assert dialog.tty_for_pid(-1) is None
    assert dialog.tty_for_pid("nonsense") is None
    assert helper.calls == []


# --- the press ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_press_names_the_console_it_was_identified_by(helper):
    assert await dialog.answer(4242, "yes") == dialog.SENT
    assert helper.calls == [("identify", "4242"), ("press", "4242", "return", "console:0x1a2b")]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw, sent", [("escape", "escape"), ("no", "escape"), ("3", "3")])
async def test_only_the_normalized_key_is_handed_over(helper, raw, sent):
    await dialog.answer(4242, raw)
    assert helper.calls[-1][2] == sent


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["", "rm -rf /", "10", "y\nrm", "hello"])
async def test_a_refused_key_never_reaches_the_helper(helper, raw):
    assert await dialog.answer(4242, raw) == dialog.BAD_KEY
    assert helper.calls == []


@pytest.mark.asyncio
async def test_no_console_presses_nothing(helper):
    helper.replies["identify"] = "none"
    assert await dialog.answer(4242, "return") == dialog.NO_TTY
    assert [c[0] for c in helper.calls] == ["identify"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reply, outcome", [
    ("gone", dialog.NOT_FOUND),       # the console closed since the lookup
    ("moved", dialog.NOT_FOUND),      # the pid is on another console now
    ("refused", dialog.BAD_KEY),
    ("", dialog.FAILED),              # the helper died or timed out
    ("something odd", dialog.FAILED),
])
async def test_every_helper_answer_maps_to_an_outcome(helper, reply, outcome):
    helper.replies["press"] = reply
    assert await dialog.answer(4242, "return") == outcome


@pytest.mark.asyncio
async def test_answer_never_raises_on_windows_either(monkeypatch):
    monkeypatch.setattr(dialog, "_WINDOWS", True)

    def boom(*a, **k):
        raise RuntimeError("helper exploded")

    monkeypatch.setattr(dialog, "_helper", boom)
    assert await dialog.answer(4242, "return") == dialog.FAILED


def test_the_helper_is_run_isolated_windowless_and_bounded(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="console:0x10\n", stderr="")

    monkeypatch.setattr(dialog.subprocess, "run", fake_run)
    assert dialog._helper("identify", "7") == "console:0x10"
    assert seen["argv"] == [sys.executable, "-I", dialog._HELPER, "identify", "7"]
    assert seen["kwargs"]["timeout"] == dialog._HELPER_TIMEOUT
    assert seen["kwargs"]["creationflags"] & subprocess.CREATE_NO_WINDOW
    assert "shell" not in seen["kwargs"]


def test_a_helper_that_times_out_is_an_empty_answer(monkeypatch):
    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(dialog.subprocess, "run", slow)
    assert dialog._helper("identify", "7") == ""


# --- the helper's own refusals, without attaching to anything ----------------

def test_the_helper_refuses_a_key_outside_the_vocabulary_before_attaching(monkeypatch):
    monkeypatch.setattr(dialog_windows, "_attach",
                        lambda pid: pytest.fail("attached for a refused key"))
    for key in ("x", "enter", "10", "", "return\r"):
        assert dialog_windows.press(4242, key, "console:0x1") == "refused"


def test_the_helper_refuses_malformed_arguments():
    assert dialog_windows.main([]) == 2
    assert dialog_windows.main(["press", "notapid", "return", "console:0x1"]) == 2
    assert dialog_windows.main(["type", "4242", "hello"]) == 2


def test_the_vocabularies_agree():
    """Every key dialog.py can hand over, the helper can press, and no more."""
    keys = {dialog.normalize_key(k) for k in ("return", "escape", *"123456789")}
    assert keys == set(dialog_windows._KEYS)


# --- the real thing, into a console these tests own --------------------------

def _reader(tmp_path, mode):
    """A child in a new console that writes down the first three keys it reads.
    `classic` reads console input events; `vt` reads raw bytes with VT input
    on, the way Node and Bun read a TTY in raw mode."""
    out = tmp_path / "keys.txt"
    if mode == "classic":
        body = "import msvcrt\nread = lambda: repr(msvcrt.getwch())\n"
    else:
        body = ("import ctypes, os\n"
                "k = ctypes.windll.kernel32\n"
                "k.SetConsoleMode(k.GetStdHandle(-10), 0x0200)\n"
                "read = lambda: repr(os.read(0, 16).decode())\n")
    script = tmp_path / "reader.py"
    script.write_text(body + (
        f"out = open({str(out)!r}, 'w')\n"
        "out.write('ready\\n'); out.flush()\n"
        "for _ in range(3):\n"
        "    out.write(read() + '\\n'); out.flush()\n"), encoding="utf-8")
    proc = subprocess.Popen([sys._base_executable, "-I", str(script)],
                            creationflags=subprocess.CREATE_NEW_CONSOLE)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if out.exists() and out.read_text().startswith("ready"):
            return proc, out
        time.sleep(0.1)
    proc.kill()
    pytest.fail("the reader never started")


@pytest.mark.browser
@pytest.mark.parametrize("mode", ["classic", "vt"])
def test_the_real_helper_presses_into_the_console_it_was_given(tmp_path, mode):
    proc, out = _reader(tmp_path, mode)
    try:
        console = dialog.console_for_pid(proc.pid)
        assert console and console.startswith("console:0x")
        assert dialog._helper("press", str(proc.pid), "return", "console:0x1") == "moved"
        for key in ("return", "escape", "7"):
            assert dialog._helper("press", str(proc.pid), key, console) == "ok"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(out.read_text().split()) < 4:
            time.sleep(0.1)
        assert out.read_text().split()[1:] == ["'\\r'", "'\\x1b'", "'7'"]
    finally:
        proc.kill()


@pytest.mark.browser
def test_the_real_helper_finds_no_console_for_a_dead_pid():
    proc = subprocess.Popen([sys._base_executable, "-I", "-c", "pass"],
                            creationflags=subprocess.CREATE_NO_WINDOW)
    proc.wait()
    assert dialog.console_for_pid(proc.pid) is None
