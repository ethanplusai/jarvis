"""JARVIS's eyes on the Mac itself: the window list, and one deliberate picture.

Two capabilities, priced very differently, exactly as `read_page` and
`look_at_page` are for the web:

1. `list_windows()` — which app is in front and what its windows are called.
   One AppleScript, a few hundred bytes, no pixels. "What am I looking at" is
   usually answerable from this alone.
2. `capture_screen()` — a PNG of the main display, shrunk, which the brain
   SEES. It reaches a `claude -p` process as an MCP `image` content block:
   the brain runs with `--tools` set to an allowlist of JARVIS's own tools and
   has no Read tool, so a path to a file would be a string it could do nothing
   with. `server.ToolImage` and `jarvis_mcp._image_block` are that route; this
   module is only a new source of pixels for it.

A version of this shipped in the first release and was deleted for being dead
code that routed through the Anthropic vision API. Two things are lifted from
it: the `screencapture -x` invocation and the System Events window script.
The API call is not — the brain runs on the user's subscription, on this
machine, and nothing here leaves it.

**This is a camera pointed at the user's life.** A screenshot can hold a
password, a private message, a client's data. So:

- Nothing here runs on a timer, speculatively, or as ambient context. The
  original's `format_windows_for_context()` fed screen state into every turn;
  that pattern does not come back. The tools are gated to a user-origin turn
  in `server.ACTING_TOOLS`, and this module is called from nowhere else.
- The capture lives in a `mkdtemp` directory for as long as it takes to shrink
  it and read the bytes — milliseconds — and the directory is removed in a
  `finally`, on every path out, success or failure.
- One display per call: the main one by default, or the display asked for.
  `screencapture` with neither `-m` nor `-D` writes one file PER display, so
  the flag is what makes the single file we read back a known screen rather
  than whichever one it happened to write first.

No new dependencies: `screencapture`, `sips` and `osascript`, each spawned
with an argument list (never a shell string) and each time-boxed. The one
permission probe is `CGPreflightScreenCaptureAccess` through `ctypes`, which
is the standard library.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import logging
import shutil
import struct
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("jarvis.screen")
_windows = sys.platform == "win32"

# Each of these must finish WELL inside `jarvis_mcp.TIMEOUT_SEC` (20s), and
# the caller puts its own hard deadline on top: a handler that outlives it
# tells the brain the server is unreachable while the work carries on anyway.
CAPTURE_TIMEOUT_SEC = 8.0
RESIZE_TIMEOUT_SEC = 6.0
WINDOWS_TIMEOUT_SEC = 5.0

# The longest edge the brain is shown. Images are charged by AREA — roughly
# width*height/750 tokens — so a Retina capture is not a neutral thing to put
# in a context this project already budgets and rotates:
#
#   1920x1080 (measured, this machine)  ~2,765 tokens
#   1280x720  (what is actually sent)   ~1,229 tokens
#   1024x576                              ~786 tokens
#
# 1280 is the same width `browser.LOOK_VIEWPORT` uses, and on a 2x Retina
# display it is exactly one image pixel per CSS pixel — the point below which
# terminal text and menu bars stop being legible, which would make the whole
# capability a confident guess. Downscaling is `sips`, which ships with macOS.
SHOT_MAX_EDGE = 1280

# A PNG bigger than this is not going through the tool channel; say so rather
# than sending something the CLI will choke on. Same bound as browser.py.
MAX_SHOT_BYTES = 4_000_000

# The window list is a tool result like any other, and every one of those is
# cut at `server.TOOL_RESULT_CAP`. Bound the list here so the cut never lands
# mid-way through the untrusted block the caller wraps it in.
MAX_WINDOWS = 12

# The blank-frame check downsamples to this edge before looking at pixels:
# small enough to be free, large enough that a real screenshot is obviously
# not one flat colour.
BLANK_SAMPLE_EDGE = 32

# Per-channel mean absolute deviation, in 0-255 levels, below which a frame
# is "one colour". Measured on this machine: a real 1280x720 screenshot's
# channels sit around 20; a solid fill is exactly 0.
BLANK_MAD = 1.5


class ScreenError(Exception):
    """Something JARVIS could not see. The message is meant to be spoken."""


@dataclass
class Shot:
    png: bytes
    width: int
    height: int


@dataclass
class Window:
    app: str
    title: str
    frontmost: bool


# ── the one subprocess boundary ────────────────────────────────────────────

async def _run(*args: str, timeout: float) -> tuple[int, str, str]:
    """Run one command, bounded by `timeout`. Never raises.

    The single seam every process in this module is spawned through, so tests
    mock one thing instead of `asyncio.create_subprocess_exec` per call — the
    pattern `preflight.py` and `dialog.py` already use. Argument lists only:
    nothing here is ever a shell string.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        return -1, "", f"failed to spawn {args[0] if args else '?'}: {e}"

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.communicate()
        except Exception:
            pass
        return -1, "", f"{args[0] if args else '?'} timed out after {timeout}s"

    return (proc.returncode if proc.returncode is not None else -1,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"))


# ── permission ─────────────────────────────────────────────────────────────

def screen_recording_granted() -> bool | None:
    """True, False, or None when the probe itself could not be run.

    `CGPreflightScreenCaptureAccess` is the documented, NON-prompting check —
    its sibling `CGRequestScreenCaptureAccess` puts a system dialog in front
    of the user, which neither a startup check nor a tool call may do.
    Reached through `ctypes` (standard library), so this costs no dependency.

    macOS attributes the permission to the app that LAUNCHED JARVIS —
    Terminal.app, Ghostty, whatever — not to python, exactly as it does with
    Accessibility. That is what `preflight.py`'s remedy explains.

    Never raises: on a macOS that has moved the symbol, or off macOS
    altogether, this is None and the caller carries on to the blank-frame
    check rather than refusing a capture that would have worked.
    """
    if sys.platform != "darwin":
        return None
    try:
        path = ctypes.util.find_library("CoreGraphics")
        if not path:
            return None
        core = ctypes.CDLL(path)
        probe = core.CGPreflightScreenCaptureAccess
        probe.restype = ctypes.c_bool
        probe.argtypes = []
        return bool(probe())
    except Exception as e:                       # pragma: no cover - defensive
        log.warning(f"screen recording probe failed: {e}")
        return None


_NO_PERMISSION = (
    "I've not been granted Screen Recording, sir, so I can't see your screen")


# ── PNG and BMP, read with nothing but the standard library ────────────────

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _png_size(png: bytes) -> tuple[int, int] | None:
    """(width, height) from a PNG's IHDR, or None if that is not a PNG.

    Doubles as the answer to "did `screencapture` actually write a picture?":
    it can exit 0 having written something that is not one.
    """
    if len(png) < 24 or not png.startswith(_PNG_MAGIC) or png[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", png[16:24])
    if width <= 0 or height <= 0:
        return None
    return width, height


def _bmp_pixels(raw: bytes) -> list[tuple[int, int, int]]:
    """The (b, g, r) triples out of an uncompressed BMP, or [] if unreadable.

    `sips` writes the sample, so this only has to read what `sips` writes:
    a BITMAPV4-ish header whose pixel offset is at byte 10 and whose bit
    depth is at byte 28, then rows of 24- or 32-bit pixels.
    """
    if len(raw) < 32 or raw[:2] != b"BM":
        return []
    offset = struct.unpack_from("<I", raw, 10)[0]
    depth = struct.unpack_from("<H", raw, 28)[0]
    if depth not in (24, 32) or offset >= len(raw):
        return []
    step = depth // 8
    body = raw[offset:]
    return [(body[i], body[i + 1], body[i + 2])
            for i in range(0, len(body) - step + 1, step)]


def _is_blank(pixels: list[tuple[int, int, int]]) -> bool:
    """Is this frame one flat colour?

    Judged PER CHANNEL. A solid dark-green desktop is (44, 62, 24) everywhere:
    spread measured across all the bytes at once calls that busy, because the
    three channels differ from each other. Spread WITHIN each channel is 0,
    which is the truth.

    A little noise around black still counts as blank — an almost-black frame
    is not a screenshot either.
    """
    if not pixels:
        return False                     # nothing to judge: do not accuse
    for channel in range(3):
        values = [p[channel] for p in pixels]
        mean = sum(values) / len(values)
        mad = sum(abs(v - mean) for v in values) / len(values)
        if mad >= BLANK_MAD:
            return False
    return True


async def _frame_is_blank(path: Path, workdir: Path) -> bool:
    """Whether the capture came back all one colour.

    Without Screen Recording, `screencapture` does not fail loudly — it exits
    0 and can hand back a black frame. A tool that returns a black rectangle
    and lets JARVIS confidently describe nothing is the worst failure this
    project has: a confident answer about an empty picture.

    `sips` does the decoding (a 32px BMP), so no image library is needed and
    no PNG variant has to be parsed here. If sips itself will not run, this
    says False: refusing a good capture over a tooling failure is worse than
    leaning on the permission probe, which has already been asked.
    """
    sample = workdir / "sample.bmp"
    rc, _out, err = await _run(
        "sips", "-Z", str(BLANK_SAMPLE_EDGE), "-s", "format", "bmp",
        "--out", str(sample), str(path), timeout=RESIZE_TIMEOUT_SEC)
    if rc != 0 or not sample.exists():
        log.warning(f"blank-frame check could not run: {err.strip()[:120]}")
        return False
    try:
        return _is_blank(_bmp_pixels(sample.read_bytes()))
    except OSError:
        return False


# ── the picture ────────────────────────────────────────────────────────────

async def capture_screen(display: int | None = None) -> Shot:
    """A PNG of one display, shrunk to `SHOT_MAX_EDGE`.

    `display` is a 1-based index as `screencapture -D` counts them; None means
    the main display. Without it a two-screen desk could only ever be asked
    about one of its screens, and JARVIS looked like he had lost sight of the
    other one.

    Raises ScreenError — with a sentence fit to be spoken — rather than ever
    handing back something the brain would describe wrongly.

    Call this ONLY on a turn the user drove. See the module docstring.
    """
    if _windows:
        import windows_desktop
        try:
            return Shot(**await windows_desktop.capture_screen(display))
        except Exception as error:
            raise ScreenError(f"I couldn't capture your Windows desktop: {error}") from error
    if screen_recording_granted() is False:
        raise ScreenError(_NO_PERMISSION)

    workdir = Path(tempfile.mkdtemp(prefix="jarvis-screen-"))
    try:
        shot_path = workdir / "screen.png"
        # -x: no shutter sound. -m: the MAIN display only. -D N: that display.
        # Never bare `screencapture`: with neither flag it writes one file PER
        # display and the single path we read back would be a lottery.
        where = ["-D", str(display)] if display else ["-m"]
        rc, _out, err = await _run("screencapture", "-x", *where, str(shot_path),
                                   timeout=CAPTURE_TIMEOUT_SEC)
        if rc != 0 or not shot_path.exists():
            log.warning(f"screencapture failed: {err.strip()[:200]}")
            raise ScreenError("I couldn't get a picture of your screen, sir")

        png = shot_path.read_bytes()
        size = _png_size(png)
        if size is None:
            raise ScreenError("I couldn't get a picture of your screen, sir")

        if max(size) > SHOT_MAX_EDGE:
            small_path = workdir / "small.png"
            rc, _out, err = await _run(
                "sips", "-Z", str(SHOT_MAX_EDGE), "--out", str(small_path),
                str(shot_path), timeout=RESIZE_TIMEOUT_SEC)
            small = small_path.read_bytes() if small_path.exists() else b""
            small_size = _png_size(small) if small else None
            if rc != 0 or small_size is None:
                log.warning(f"sips could not resize the capture: {err.strip()[:200]}")
                # Deliberately NOT sending the full-size one instead: a
                # 3024-wide capture is thousands of tokens off one turn.
                raise ScreenError(
                    "I couldn't get your screen down to a sensible size, sir")
            shot_path, png, size = small_path, small, small_size

        if len(png) > MAX_SHOT_BYTES:
            raise ScreenError("that picture came out far too large to send, sir")

        if await _frame_is_blank(shot_path, workdir):
            raise ScreenError(
                "your screen came back blank, sir — which usually means Screen "
                "Recording isn't granted rather than that there's nothing there")

        return Shot(png=png, width=size[0], height=size[1])
    finally:
        # The capture is on disk for as long as this takes and no longer.
        shutil.rmtree(workdir, ignore_errors=True)


# ── the cheap path ─────────────────────────────────────────────────────────

# Lifted from the original screen.py, with the bug it shipped with fixed.
# Read-only: it enumerates processes that are already running and opens
# nothing. Per-app lookups stay inside a `try` so one uncooperative app cannot
# cost the whole list -- but the FIRST window read is deliberately unguarded.
#
# Measured on this machine: without Accessibility, `windows of proc` fails for
# EVERY process (-1728 / -25211), and the original's blanket `try` swallowed
# all of them, so the script exited 0 with an empty string. JARVIS would then
# say "there are no windows open, sir" with nine apps running -- a confident
# answer about nothing, which is the failure this project has hit all night.
# Finder is always running, so the unguarded probe below turns that silence
# into the loud error `list_windows` can refuse on.
_WINDOWS_SCRIPT = """
set windowList to ""
tell application "System Events"
    set frontApp to name of first application process whose frontmost is true
    count of windows of application process "Finder"
    set visibleApps to every application process whose visible is true
    repeat with proc in visibleApps
        set appName to name of proc
        try
            repeat with w in (windows of proc)
                try
                    set winTitle to name of w
                    if winTitle is not "" and winTitle is not missing value then
                        set windowList to windowList & appName & "|||" & winTitle & "|||" & (appName = frontApp) & linefeed
                    end if
                end try
            end repeat
        end try
    end repeat
end tell
return windowList
"""

# macOS's own wording and error codes for "Accessibility not granted".
# Mirrors preflight._ACCESSIBILITY_MARKERS, which sees the same refusal for a
# sibling System Events call.
_ACCESSIBILITY_MARKERS = ("-1728", "-1719", "-25211",
                          "not allowed assistive access")


async def list_windows() -> list[Window]:
    """Open windows: app name, window title, and which app is in front.

    Raises ScreenError when Accessibility is missing. An empty list would have
    JARVIS say "nothing is open" — a lie with a remedy attached.
    """
    if _windows:
        import windows_desktop
        try:
            return [Window(**row) for row in await windows_desktop.list_windows()]
        except Exception as error:
            raise ScreenError(f"I couldn't read your Windows windows: {error}") from error
    rc, stdout, stderr = await _run("osascript", "-e", _WINDOWS_SCRIPT,
                                    timeout=WINDOWS_TIMEOUT_SEC)
    if rc != 0:
        combined = f"{stdout}\n{stderr}".lower()
        if any(m in combined for m in _ACCESSIBILITY_MARKERS):
            raise ScreenError(
                "I've not been granted Accessibility, sir, so I can't read "
                "your window titles")
        log.warning(f"list_windows failed: {stderr.strip()[:200]}")
        raise ScreenError("I couldn't read what's open, sir")

    windows: list[Window] = []
    for line in stdout.splitlines():
        parts = line.split("|||")
        if len(parts) < 3:
            continue
        windows.append(Window(app=parts[0].strip(), title=parts[1].strip(),
                              frontmost=parts[2].strip().lower() == "true"))
        if len(windows) >= MAX_WINDOWS:
            break
    return windows
