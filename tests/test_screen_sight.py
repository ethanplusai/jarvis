"""Seeing the user's screen — the cheap window list, and one deliberate picture.

The user, three times: "okay I ran it can you see my screen", "can you see my
screen if I pull it up", "we definitely need to give him ability to see the
screen and process it."

`screen.py` shipped in the first release and was deleted for being dead code
that routed through the Anthropic vision API. This is the same two
capabilities rebuilt on the subscription path: the window list (AppleScript,
a few hundred bytes) and a screenshot the brain SEES, which reaches it as an
MCP `image` content block — the one route into a `claude -p` process that has
no Read tool. `screencapture` and `sips` and nothing else.

NOTHING here captures the real screen. `screen._run` is the single subprocess
seam, and the fake writes the files a real `screencapture`/`sips` would.
The two exceptions are marked: they drive the REAL `sips` against synthetic
PNGs built here with zlib, because a blank-frame detector that has never seen
a real encoder is a detector that has never been tested.
"""

import asyncio
import base64
import importlib
import struct
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import screen as real_screen


# --- building real PNGs, with nothing but the standard library -------------

def _png(width: int, height: int, rows) -> bytes:
    """A real 8-bit RGB PNG. `rows(y, x) -> (r, g, b)`."""
    raw = b"".join(
        b"\x00" + b"".join(bytes(rows(y, x)) for x in range(width))
        for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def _solid(width, height, colour):
    return _png(width, height, lambda y, x: colour)


def _busy(width, height):
    """Something with real structure in it, the way a screenshot has."""
    return _png(width, height,
                lambda y, x: (((x * 7) + y) % 256, (x * 3) % 256, (y * 11) % 256))


BLACK = _solid(64, 40, (0, 0, 0))
BUSY = _busy(1920, 1080)


# --- the subprocess seam ---------------------------------------------------

class _Runner:
    """Stands in for `screencapture` and `sips`. Records argv, writes files."""

    def __init__(self):
        self.calls: list[tuple[str, ...]] = []
        self.capture_png = BUSY          # what screencapture "takes"
        self.resized_png = _busy(1280, 720)
        self.capture_rc = 0
        self.resize_rc = 0
        self.write_capture = True
        self.stall = False
        self.bmp = None                  # None -> derived from the resized PNG

    async def __call__(self, *args, timeout):
        self.calls.append(tuple(args))
        if self.stall:
            await asyncio.sleep(30)
        if args[0] == "screencapture":
            if self.write_capture and self.capture_rc == 0:
                Path(args[-1]).write_bytes(self.capture_png)
            return self.capture_rc, "", ""
        if args[0] == "sips":
            out = Path(args[args.index("--out") + 1])
            if "bmp" in args:
                out.write_bytes(self.bmp if self.bmp is not None
                                else _bmp_of(self.resized_png))
                return 0, "", ""
            if self.resize_rc == 0:
                out.write_bytes(self.resized_png)
            return self.resize_rc, "", ""
        raise AssertionError(f"unexpected command: {args!r}")


def _bmp_of(png: bytes) -> bytes:
    """A 32x32-ish BMP whose pixels vary, as sips would produce from a
    busy PNG. Only the header fields screen.py reads are real."""
    pixels = bytes(bytearray((i * 13) % 256 for i in range(32 * 32 * 3)))
    return _bmp(pixels, bpp=24)


def _bmp(pixels: bytes, bpp: int = 24) -> bytes:
    header = bytearray(54)
    header[0:2] = b"BM"
    struct.pack_into("<I", header, 10, 54)      # pixel data offset
    struct.pack_into("<I", header, 18, 32)      # width
    struct.pack_into("<i", header, 22, -32)     # height, top-down
    struct.pack_into("<H", header, 28, bpp)
    return bytes(header) + pixels


@pytest.fixture(autouse=True)
def _the_macos_half(monkeypatch):
    """Everything in this file drives the macOS half through its `_run` seam.
    On Windows `screen` hands both calls to `screen_windows` instead, which
    test_screen_windows.py covers."""
    monkeypatch.setattr(real_screen, "_WINDOWS", False)


@pytest.fixture
def runner(monkeypatch):
    fake = _Runner()
    monkeypatch.setattr(real_screen, "_run", fake)
    monkeypatch.setattr(real_screen, "screen_recording_granted", lambda: True)
    return fake


# --- reading a PNG's own header -------------------------------------------

def test_a_pngs_size_is_read_from_its_header():
    assert real_screen._png_size(_solid(37, 19, (1, 2, 3))) == (37, 19)


def test_something_that_is_not_a_png_has_no_size():
    """`screencapture` exiting 0 having written junk must not be mistaken for
    a picture."""
    assert real_screen._png_size(b"not a png at all") is None
    assert real_screen._png_size(b"") is None


# --- what is captured ------------------------------------------------------

@pytest.mark.asyncio
async def test_the_main_display_is_captured_silently_with_an_argument_list(runner):
    await asyncio.wait_for(real_screen.capture_screen(), 5)
    cmd = runner.calls[0]
    assert cmd[0] == "screencapture"
    assert "-x" in cmd, "-x suppresses the shutter sound"
    assert "-m" in cmd, "the MAIN display only: other displays are not ours to take"
    assert cmd[-1].endswith(".png")
    assert all(isinstance(a, str) for a in cmd)


@pytest.mark.asyncio
async def test_a_retina_capture_is_shrunk_before_it_is_sent(runner):
    """Every pixel becomes tokens. A 1920- or 3024-wide capture is not
    something to put in the brain's context at full size."""
    shot = await asyncio.wait_for(real_screen.capture_screen(), 5)
    resize = [c for c in runner.calls if c[0] == "sips" and "-Z" in c][0]
    assert resize[resize.index("-Z") + 1] == str(real_screen.SHOT_MAX_EDGE)
    assert shot.width == 1280 and shot.height == 720
    assert shot.png == runner.resized_png, "the big one went instead of the small one"


@pytest.mark.asyncio
async def test_a_screen_already_within_budget_is_not_resized(runner):
    runner.capture_png = _busy(1024, 640)
    shot = await asyncio.wait_for(real_screen.capture_screen(), 5)
    assert not [c for c in runner.calls if c[0] == "sips" and "-Z" in c
                and "bmp" not in c]
    assert (shot.width, shot.height) == (1024, 640)


@pytest.mark.asyncio
async def test_a_capture_that_cannot_be_shrunk_is_refused_not_sent_whole(runner):
    """Failing open here would quietly spend thousands of tokens on one turn."""
    runner.resize_rc = 1
    with pytest.raises(real_screen.ScreenError):
        await asyncio.wait_for(real_screen.capture_screen(), 5)


@pytest.mark.asyncio
async def test_screencapture_failing_is_said_not_swallowed(runner):
    runner.capture_rc = 1
    runner.write_capture = False
    with pytest.raises(real_screen.ScreenError) as caught:
        await asyncio.wait_for(real_screen.capture_screen(), 5)
    assert "screen" in str(caught.value).lower()


@pytest.mark.asyncio
async def test_a_picture_too_big_for_the_tool_channel_is_refused(runner, monkeypatch):
    monkeypatch.setattr(real_screen, "MAX_SHOT_BYTES", 100)
    with pytest.raises(real_screen.ScreenError) as caught:
        await asyncio.wait_for(real_screen.capture_screen(), 5)
    assert "large" in str(caught.value)


@pytest.mark.asyncio
async def test_every_subprocess_is_time_boxed(runner):
    """A wedged `screencapture` (a locked screen, a hung window server) must
    not hold the tool channel open past its own timeout."""
    class _Timed(_Runner):
        def __init__(self):
            super().__init__()
            self.timeouts = []

        async def __call__(self, *args, timeout):
            self.timeouts.append(timeout)
            return await _Runner.__call__(self, *args, timeout=timeout)

    timed = _Timed()
    real_screen._run = timed
    try:
        await asyncio.wait_for(real_screen.capture_screen(), 5)
    finally:
        real_screen._run = runner
    assert timed.timeouts and all(0 < t <= 10 for t in timed.timeouts)


@pytest.mark.asyncio
async def test_a_stalled_subprocess_is_killed_and_reported():
    """The real `_run`, against a real process that will not finish."""
    rc, _out, err = await asyncio.wait_for(
        real_screen._run(sys.executable, "-c", "import time; time.sleep(30)",
                         timeout=0.2), 5)
    assert rc == -1
    assert "timed out" in err


# --- permission, honestly --------------------------------------------------

@pytest.mark.asyncio
async def test_nothing_is_captured_at_all_without_screen_recording(runner,
                                                                  monkeypatch):
    monkeypatch.setattr(real_screen, "screen_recording_granted", lambda: False)
    with pytest.raises(real_screen.ScreenError) as caught:
        await asyncio.wait_for(real_screen.capture_screen(), 5)
    assert "Screen Recording" in str(caught.value)
    assert runner.calls == [], "it ran screencapture anyway"


@pytest.mark.asyncio
async def test_an_undeterminable_permission_still_tries(runner, monkeypatch):
    """`None` means the probe itself failed, not that permission is missing.
    Refusing then would break seeing the screen on any Mac the probe cannot
    read — the blank-frame check below is the backstop."""
    monkeypatch.setattr(real_screen, "screen_recording_granted", lambda: None)
    shot = await asyncio.wait_for(real_screen.capture_screen(), 5)
    assert shot.png


@pytest.mark.asyncio
async def test_a_blank_frame_is_refused_rather_than_described(runner):
    """Without Screen Recording `screencapture` does not fail loudly — it can
    hand back a black or desktop-only frame. A tool that returns a black
    rectangle and lets JARVIS confidently describe nothing is the exact
    failure this project has hit all night."""
    runner.bmp = _bmp(bytes(32 * 32 * 3))          # every pixel black
    with pytest.raises(real_screen.ScreenError) as caught:
        await asyncio.wait_for(real_screen.capture_screen(), 5)
    said = str(caught.value)
    assert "Screen Recording" in said
    assert "blank" in said or "empty" in said


@pytest.mark.asyncio
async def test_one_flat_colour_is_blank_too_not_just_black(runner):
    runner.bmp = _bmp(b"\x2c\x3e\x18" * (32 * 32))
    with pytest.raises(real_screen.ScreenError):
        await asyncio.wait_for(real_screen.capture_screen(), 5)


def test_blankness_is_judged_per_channel_not_across_them():
    """A solid dark-green desktop has three DIFFERENT channel values, so a
    naive spread over all the bytes at once calls it busy. It is not."""
    flat = [(44, 62, 24)] * 400
    assert real_screen._is_blank(flat) is True
    assert real_screen._is_blank([(0, 0, 0)] * 400) is True
    assert real_screen._is_blank([((i * 7) % 256, (i * 3) % 256, i % 256)
                                  for i in range(400)]) is False


def test_near_black_noise_is_still_blank():
    """A frame that is black bar a little encoder noise is not a screenshot."""
    assert real_screen._is_blank([(i % 2, 0, i % 2) for i in range(400)]) is True


@pytest.mark.asyncio
async def test_a_blank_check_that_cannot_run_does_not_block_a_good_capture(
        runner, monkeypatch):
    """sips refusing to make the sample tells us nothing about the picture.
    Refusing every capture on a tooling failure is worse than the permission
    probe already having said yes."""
    async def _run(*args, timeout):
        if args[0] == "sips" and "bmp" in args:
            return 1, "", "sips: cannot do that"
        return await runner(*args, timeout=timeout)

    monkeypatch.setattr(real_screen, "_run", _run)
    shot = await asyncio.wait_for(real_screen.capture_screen(), 5)
    assert shot.png


# --- the real sips, on real PNGs -------------------------------------------

@pytest.mark.skipif(sys.platform != "darwin", reason="sips is macOS's")
@pytest.mark.asyncio
async def test_the_real_sips_sees_a_real_black_png_as_blank(tmp_path):
    path = tmp_path / "black.png"
    path.write_bytes(_solid(400, 300, (0, 0, 0)))
    assert await asyncio.wait_for(
        real_screen._frame_is_blank(path, tmp_path), 10) is True


@pytest.mark.skipif(sys.platform != "darwin", reason="sips is macOS's")
@pytest.mark.asyncio
async def test_the_real_sips_sees_a_busy_png_as_not_blank(tmp_path):
    path = tmp_path / "busy.png"
    path.write_bytes(_busy(400, 300))
    assert await asyncio.wait_for(
        real_screen._frame_is_blank(path, tmp_path), 10) is False


# --- nothing is left on disk ----------------------------------------------

@pytest.mark.asyncio
async def test_the_capture_is_deleted_the_moment_it_has_been_read(runner,
                                                                  monkeypatch):
    """A screenshot can hold a password, a private message, a client's data.
    It lives in a temp directory for the milliseconds it takes to shrink and
    read, and then it is gone."""
    seen = []
    real_mkdtemp = real_screen.tempfile.mkdtemp

    def _spy(*a, **k):
        d = real_mkdtemp(*a, **k)
        seen.append(Path(d))
        return d

    monkeypatch.setattr(real_screen.tempfile, "mkdtemp", _spy)
    shot = await asyncio.wait_for(real_screen.capture_screen(), 5)
    assert shot.png
    assert seen and not seen[0].exists(), "the screenshot is still on disk"


@pytest.mark.asyncio
async def test_the_capture_is_deleted_even_when_the_capture_fails(runner,
                                                                 monkeypatch):
    seen = []
    real_mkdtemp = real_screen.tempfile.mkdtemp

    def _spy(*a, **k):
        d = real_mkdtemp(*a, **k)
        seen.append(Path(d))
        return d

    monkeypatch.setattr(real_screen.tempfile, "mkdtemp", _spy)
    runner.bmp = _bmp(bytes(32 * 32 * 3))
    with pytest.raises(real_screen.ScreenError):
        await asyncio.wait_for(real_screen.capture_screen(), 5)
    assert seen and not seen[0].exists()


# --- the cheap path: what is on screen without any pixels ------------------

@pytest.mark.asyncio
async def test_the_window_list_names_the_app_the_title_and_which_is_front(
        monkeypatch):
    async def _run(*args, timeout):
        assert args[0] == "osascript"
        return 0, ("Ghostty|||jarvis — main|||true\n"
                   "Chrome|||Dashboard|||false\n"), ""

    monkeypatch.setattr(real_screen, "_run", _run)
    windows = await asyncio.wait_for(real_screen.list_windows(), 5)
    assert [(w.app, w.title, w.frontmost) for w in windows] == [
        ("Ghostty", "jarvis — main", True),
        ("Chrome", "Dashboard", False)]


@pytest.mark.asyncio
async def test_the_window_list_is_bounded(monkeypatch):
    async def _run(*args, timeout):
        return 0, "".join(f"App{i}|||Window {i}|||false\n" for i in range(200)), ""

    monkeypatch.setattr(real_screen, "_run", _run)
    windows = await asyncio.wait_for(real_screen.list_windows(), 5)
    assert len(windows) == real_screen.MAX_WINDOWS


@pytest.mark.skipif(sys.platform != "darwin", reason="osascript is macOS's")
@pytest.mark.asyncio
@pytest.mark.browser
async def test_the_real_script_never_answers_an_empty_desk_silently():
    """The bug the original `get_active_windows()` shipped with, measured on
    this machine: without Accessibility, `windows of proc` fails for EVERY
    process, and a script that wraps each read in `try` swallows all of them —
    exit 0, empty output. JARVIS then says "there are no windows open, sir"
    while nine apps are running. The script must fail LOUDLY instead.

    This drives the real `osascript`, because it is the real `osascript` that
    decides. It passes either way: on a machine with Accessibility it gets
    windows; on one without, it must get an error, never silence.
    """
    rc, out, err = await asyncio.wait_for(
        real_screen._run("osascript", "-e", real_screen._WINDOWS_SCRIPT,
                         timeout=10), 15)
    lines = [ln for ln in out.splitlines() if "|||" in ln]
    if rc == 0 and lines:
        return                                  # Accessibility is granted
    if rc != 0:
        assert "assistive access" in f"{out}{err}".lower(), \
            f"failed for some other reason: {err.strip()[:200]}"
        return
    # Exit 0 and nothing to show: only honest if the desk really is empty.
    _rc, apps, _err = await asyncio.wait_for(
        real_screen._run(
            "osascript", "-e",
            'tell application "System Events" to get name of every '
            'application process whose visible is true', timeout=10), 15)
    assert not apps.strip(), (
        "empty window list while these apps are visible — the denial was "
        f"swallowed: {apps.strip()[:120]}")


@pytest.mark.parametrize("code", ["-1728", "-1719", "-25211"])
@pytest.mark.asyncio
async def test_no_accessibility_is_said_plainly_not_reported_as_an_empty_desk(
        monkeypatch, code):
    """An empty list would have JARVIS say "nothing is open", which is a lie
    with a remedy attached. All three codes are ones this machine has actually
    produced for these System Events calls."""
    async def _run(*args, timeout):
        return 1, "", ("execution error: System Events got an error: osascript "
                       f"is not allowed assistive access. ({code})")

    monkeypatch.setattr(real_screen, "_run", _run)
    with pytest.raises(real_screen.ScreenError) as caught:
        await asyncio.wait_for(real_screen.list_windows(), 5)
    assert "Accessibility" in str(caught.value)


@pytest.mark.asyncio
async def test_the_window_list_is_time_boxed(monkeypatch):
    """`osascript` against System Events can sit there for a long time if an
    app is not answering."""
    seen = {}

    async def _run(*args, timeout):
        seen["timeout"] = timeout
        return 0, "", ""

    monkeypatch.setattr(real_screen, "_run", _run)
    await asyncio.wait_for(real_screen.list_windows(), 5)
    assert 0 < seen["timeout"] <= 10


# --- the permission probe itself ------------------------------------------

def test_the_permission_probe_never_raises(monkeypatch):
    """It is called on the capture path and at startup. A macOS that has
    moved the symbol must produce None, not an exception."""
    monkeypatch.setattr(real_screen.ctypes.util, "find_library",
                        lambda name: None)
    assert real_screen.screen_recording_granted() is None


def test_the_permission_probe_is_the_non_prompting_one():
    """`CGRequestScreenCaptureAccess` puts a system dialog in front of the
    user. Preflight and a tool call must never do that."""
    import ast
    import inspect
    # The symbols this module actually reaches for, not the prose about them:
    # the docstring names the prompting one in order to say it is banned.
    tree = ast.parse(inspect.getsource(real_screen))
    reached = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "CGPreflightScreenCaptureAccess" in reached
    assert "CGRequestScreenCaptureAccess" not in reached


@pytest.mark.skipif(sys.platform != "darwin", reason="CoreGraphics is macOS's")
def test_the_real_permission_probe_answers_yes_or_no():
    assert real_screen.screen_recording_granted() in (True, False)
