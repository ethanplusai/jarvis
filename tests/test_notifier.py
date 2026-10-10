"""Unit tests for notifier.py -- the macOS notification fallback.

These tests never post a real notification: `asyncio.create_subprocess_exec`
is mocked at the boundary so the developer running the suite is not spammed.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import notifier


@pytest.fixture(autouse=True)
def _macos_unless_a_test_says_otherwise(monkeypatch):
    """The osascript path is under test here whatever the suite runs on; the
    Windows path has its own tests at the bottom of this file."""
    monkeypatch.setattr(notifier, "_WINDOWS", False)


class _FakeProcess:
    """Stand-in for the object asyncio.create_subprocess_exec() returns."""

    def __init__(self, returncode=0, stdout=b"", stderr=b"", hang=False):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._hang = hang
        self.killed = False

    async def communicate(self, input=None):
        self.sent_stdin = input
        if self._hang and not self.killed:
            # Simulate a wedged osascript: never resolves on its own until
            # killed -- like a real process, communicate() after kill()
            # returns promptly instead of hanging forever.
            await asyncio.sleep(999)
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True


def _patch_subprocess(fake_proc, capture=None):
    """Patch create_subprocess_exec to return fake_proc, recording call args."""

    async def _fake_create_subprocess_exec(*args, **kwargs):
        if capture is not None:
            capture["args"] = args
            capture["kwargs"] = kwargs
        return fake_proc

    return patch("notifier.asyncio.create_subprocess_exec", side_effect=_fake_create_subprocess_exec)


@pytest.mark.asyncio
async def test_successful_post_returns_true():
    fake_proc = _FakeProcess(returncode=0)
    with patch("notifier.available", return_value=True):
        with _patch_subprocess(fake_proc):
            result = await notifier.notify("Title", "Message")
    assert result is True


@pytest.mark.asyncio
async def test_nonzero_exit_returns_false_without_raising():
    fake_proc = _FakeProcess(returncode=1, stderr=b"some applescript error")
    with patch("notifier.available", return_value=True):
        with _patch_subprocess(fake_proc):
            result = await notifier.notify("Title", "Message")
    assert result is False


@pytest.mark.asyncio
async def test_missing_osascript_returns_false():
    with patch("notifier.available", return_value=False):
        result = await notifier.notify("Title", "Message")
    assert result is False


@pytest.mark.asyncio
async def test_timeout_returns_false_without_raising():
    fake_proc = _FakeProcess(hang=True)
    with patch("notifier.available", return_value=True):
        with patch("notifier._TIMEOUT_SECONDS", 0.05):
            with _patch_subprocess(fake_proc):
                result = await notifier.notify("Title", "Message")
    assert result is False
    assert fake_proc.killed is True


@pytest.mark.asyncio
async def test_spawn_failure_returns_false_without_raising():
    async def _raise(*args, **kwargs):
        raise OSError("no such file or directory: osascript")

    with patch("notifier.available", return_value=True):
        with patch("notifier.asyncio.create_subprocess_exec", side_effect=_raise):
            result = await notifier.notify("Title", "Message")
    assert result is False


def test_available_false_on_non_darwin(monkeypatch):
    monkeypatch.setattr(notifier.sys, "platform", "linux")
    assert notifier.available() is False


def test_available_false_when_osascript_missing(monkeypatch):
    monkeypatch.setattr(notifier.sys, "platform", "darwin")
    monkeypatch.setattr(notifier.shutil, "which", lambda name: None)
    assert notifier.available() is False


def test_available_true_on_darwin_with_osascript(monkeypatch):
    monkeypatch.setattr(notifier.sys, "platform", "darwin")
    monkeypatch.setattr(notifier.shutil, "which", lambda name: "/usr/bin/osascript")
    assert notifier.available() is True


# --- Escaping / injection tests -------------------------------------------
#
# The notification text is attacker-influenced (it can carry a session
# title or last message from someone else's Claude Code transcript). The
# module's defense is to never interpolate that text into the AppleScript
# *source* at all -- it is passed as `on run argv` arguments to `osascript`,
# which delivers it to the script as inert data. These tests assert that
# the boundary call actually receives the raw, unmodified, unescaped text
# as separate argv entries, and that the script text piped to osascript's
# stdin never contains the untrusted payload. A naive implementation that
# builds the AppleScript source via string interpolation (e.g.
# f'display notification "{message}" with title "{title}"') would either
# corrupt/escape the text before it reaches the subprocess call, or bake it
# into the piped script -- and would fail one or both assertions below.

INJECTION_PAYLOAD = '"; do shell script "touch ~/PWNED"; --\\ backslash " quote \n newline'


@pytest.mark.asyncio
async def test_injection_payload_reaches_boundary_as_literal_argv():
    fake_proc = _FakeProcess(returncode=0)
    capture = {}
    with patch("notifier.available", return_value=True):
        with _patch_subprocess(fake_proc, capture=capture):
            result = await notifier.notify(INJECTION_PAYLOAD, "a normal message")

    assert result is True
    args = capture["args"]
    # Called as: osascript, "-", title, message, subtitle
    assert args[0] == "osascript"
    assert args[1] == "-"
    # The title argv entry must be the exact, byte-identical payload --
    # no escaping, no truncation (it's under the length cap), no mangling.
    assert args[2] == INJECTION_PAYLOAD

    # The script text sent over stdin must be fixed and must NOT contain
    # the untrusted payload anywhere -- it never entered the script source.
    stdin_sent = fake_proc.sent_stdin.decode("utf-8")
    assert INJECTION_PAYLOAD not in stdin_sent
    assert "do shell script" not in stdin_sent
    assert "on run argv" in stdin_sent


@pytest.mark.asyncio
async def test_quotes_and_backslashes_pass_through_unescaped_in_message():
    fake_proc = _FakeProcess(returncode=0)
    capture = {}
    payload = 'She said \\"hello\\" and left \\ trailing backslash'
    with patch("notifier.available", return_value=True):
        with _patch_subprocess(fake_proc, capture=capture):
            await notifier.notify("Title", payload)

    args = capture["args"]
    assert args[3] == payload  # message is the 4th positional arg


@pytest.mark.asyncio
async def test_long_text_is_truncated():
    fake_proc = _FakeProcess(returncode=0)
    capture = {}
    long_title = "T" * 500
    long_message = "M" * 500
    with patch("notifier.available", return_value=True):
        with _patch_subprocess(fake_proc, capture=capture):
            await notifier.notify(long_title, long_message)

    args = capture["args"]
    sent_title, sent_message = args[2], args[3]
    assert len(sent_title) <= notifier._TITLE_MAX
    assert len(sent_message) <= notifier._MESSAGE_MAX
    assert sent_title != long_title
    assert sent_message != long_message
    assert sent_title.endswith("…")
    assert sent_message.endswith("…")


# --- Windows ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_windows_text_travels_as_environment_data_never_as_script(monkeypatch):
    """The PowerShell script is fixed and base64-encoded; the untrusted text
    is only ever in the environment, where nothing parses it."""
    import base64
    monkeypatch.setattr(notifier, "_WINDOWS", True)
    payload = '"; Start-Process calc; "$(calc)<toast>'
    capture = {}
    fake_proc = _FakeProcess(returncode=0)
    with patch("notifier.available", return_value=True):
        with _patch_subprocess(fake_proc, capture):
            assert await notifier.notify("T", payload, subtitle="S") is True
    argv = capture["args"]
    assert argv[0] == "powershell" and argv[-2] == "-EncodedCommand"
    script = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert script == notifier._WINDOWS_SCRIPT
    assert all(payload not in a for a in argv)
    env = capture["kwargs"]["env"]
    assert env["JARVIS_NOTIFY_MESSAGE"] == payload
    assert (env["JARVIS_NOTIFY_TITLE"], env["JARVIS_NOTIFY_SUBTITLE"]) == ("T", "S")
    assert "CreateTextNode" in script, "inserted as text, never as markup"
