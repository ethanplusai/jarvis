"""
JARVIS cross-platform desktop control.

Pure command builders (unit-testable, no desktop required) plus thin async
executors. Every executor returns {"success": bool, "confirmation": str} and
never raises — unsupported platforms and missing binaries degrade to a polite
explanation, per the project rule that non-macOS hosts must fail gracefully.
"""

import asyncio
import logging
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

log = logging.getLogger("jarvis.system_control")

if sys.platform == "darwin":
    PLATFORM = "mac"
elif sys.platform == "win32":
    PLATFORM = "windows"
else:
    PLATFORM = "linux"

SCREENSHOTS_DIR = Path(__file__).parent / "data" / "screenshots"

MEDIA_ACTIONS = ("play_pause", "next", "previous")


def _unsupported(what: str) -> dict:
    return {
        "success": False,
        "confirmation": f"I'm afraid {what} isn't available on this platform yet, sir.",
    }


# ---------------------------------------------------------------------------
# Pure command builders — take the platform explicitly so tests need no desktop
# ---------------------------------------------------------------------------

def open_url_cmd(url: str, platform: str = PLATFORM) -> list[str] | None:
    if platform == "mac":
        return ["open", url]
    if platform == "windows":
        # Empty title argument so URLs with ampersands aren't eaten by `start`.
        return ["cmd", "/c", "start", "", url]
    return ["xdg-open", url]


def open_app_cmd(app: str, platform: str = PLATFORM) -> list[str] | None:
    if platform == "mac":
        return ["open", "-a", app]
    if platform == "windows":
        return ["cmd", "/c", "start", "", app]
    # Linux: gtk-launch resolves .desktop entries by name; the executor falls
    # back to running the slug as a binary if this fails.
    return ["gtk-launch", app.lower().replace(" ", "-")]


def open_path_cmd(path: str, platform: str = PLATFORM) -> list[str] | None:
    if platform == "mac":
        return ["open", path]
    if platform == "windows":
        return ["explorer", path]
    return ["xdg-open", path]


def set_volume_cmd(level: int, platform: str = PLATFORM) -> list[str] | None:
    level = max(0, min(100, int(level)))
    if platform == "mac":
        return ["osascript", "-e", f"set volume output volume {level}"]
    if platform == "linux":
        return ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"]
    # Windows has no clean stdlib route; degrade gracefully.
    return None


def media_cmd(action: str, platform: str = PLATFORM) -> list[str] | None:
    if action not in MEDIA_ACTIONS:
        return None
    if platform == "mac":
        verb = {"play_pause": "playpause", "next": "next track", "previous": "previous track"}[action]
        return ["osascript", "-e", f'tell application "Music" to {verb}']
    if platform == "linux":
        verb = {"play_pause": "play-pause", "next": "next", "previous": "previous"}[action]
        return ["playerctl", verb]
    return None


def lock_screen_cmd(platform: str = PLATFORM) -> list[str] | None:
    if platform == "mac":
        return ["pmset", "displaysleepnow"]
    if platform == "windows":
        return ["rundll32.exe", "user32.dll,LockWorkStation"]
    return ["loginctl", "lock-session"]


def clipboard_copy_cmd(platform: str = PLATFORM) -> list[str] | None:
    """Command that reads stdin and places it on the clipboard."""
    if platform == "mac":
        return ["pbcopy"]
    if platform == "windows":
        return ["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"]
    return ["xclip", "-selection", "clipboard"]


def clipboard_copy_fallback_cmd(platform: str = PLATFORM) -> list[str] | None:
    """Secondary clipboard tool (Wayland) tried when the primary is missing."""
    if platform == "linux":
        return ["wl-copy"]
    return None


def screenshot_cmd(out_path: str, platform: str = PLATFORM) -> list[str] | None:
    if platform == "mac":
        return ["screencapture", "-x", out_path]
    if platform == "linux":
        return ["gnome-screenshot", "-f", out_path]
    return None


def screenshot_fallback_cmd(out_path: str, platform: str = PLATFORM) -> list[str] | None:
    if platform == "linux":
        return ["import", "-window", "root", out_path]  # ImageMagick
    return None


def terminal_cmd(command: str = "", platform: str = PLATFORM) -> list[str] | None:
    """Open a terminal window, optionally running a command (non-mac only —
    macOS keeps its richer AppleScript path in actions.py)."""
    if platform == "windows":
        args = ["cmd", "/c", "start", "powershell", "-NoExit"]
        if command:
            args += ["-Command", command]
        return args
    if platform == "linux":
        if command:
            return ["x-terminal-emulator", "-e", "bash", "-lc", command]
        return ["x-terminal-emulator"]
    return None


def terminal_fallback_cmds(command: str = "", platform: str = PLATFORM) -> list[list[str]]:
    if platform != "linux":
        return []
    if command:
        return [
            ["gnome-terminal", "--", "bash", "-lc", command],
            ["konsole", "-e", "bash", "-lc", command],
        ]
    return [["gnome-terminal"], ["konsole"]]


# ---------------------------------------------------------------------------
# Async executor
# ---------------------------------------------------------------------------

async def _run(cmd: list[str], input_text: str | None = None, timeout: float = 10) -> tuple[bool, str]:
    """Run a command, returning (success, stderr_text). Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if input_text is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(
            proc.communicate(input_text.encode() if input_text is not None else None),
            timeout=timeout,
        )
        return proc.returncode == 0, stderr.decode(errors="replace").strip()
    except FileNotFoundError:
        return False, f"{cmd[0]} not found"
    except asyncio.TimeoutError:
        return False, "timed out"
    except Exception as e:  # noqa: BLE001 — desktop control must never crash the loop
        return False, str(e)


# ---------------------------------------------------------------------------
# Public actions — {"success": bool, "confirmation": str}
# ---------------------------------------------------------------------------

async def open_url(url: str) -> dict:
    cmd = open_url_cmd(url)
    if cmd:
        ok, err = await _run(cmd)
        if ok:
            return {"success": True, "confirmation": "Pulled that up in your browser, sir."}
        log.warning(f"open_url via {cmd[0]} failed: {err}")
    # Last resort: Python's webbrowser module.
    try:
        opened = await asyncio.to_thread(webbrowser.open, url)
        if opened:
            return {"success": True, "confirmation": "Pulled that up in your browser, sir."}
    except Exception as e:
        log.warning(f"webbrowser fallback failed: {e}")
    return {"success": False, "confirmation": "I couldn't reach a browser on this machine, sir."}


async def open_app(app: str) -> dict:
    app = app.strip()
    if not app:
        return {"success": False, "confirmation": "I need an application name to open, sir."}
    cmd = open_app_cmd(app)
    ok, err = await _run(cmd)
    if not ok and PLATFORM == "linux":
        # Fall back to treating the name as a binary on PATH.
        ok, err = await _run([app.lower().replace(" ", "-")])
    if ok:
        return {"success": True, "confirmation": f"{app} is opening, sir."}
    log.warning(f"open_app '{app}' failed: {err}")
    return {"success": False, "confirmation": f"I couldn't find {app} on this machine, sir."}


async def open_path(path: str) -> dict:
    target = Path(path.strip()).expanduser()
    if not target.exists():
        return {"success": False, "confirmation": f"I couldn't find {target.name or path} on disk, sir."}
    cmd = open_path_cmd(str(target))
    ok, err = await _run(cmd)
    if ok:
        return {"success": True, "confirmation": f"Opened {target.name}, sir."}
    log.warning(f"open_path '{path}' failed: {err}")
    return _unsupported("opening files")


async def set_volume(level) -> dict:
    try:
        level = max(0, min(100, int(str(level).strip().rstrip("%"))))
    except (ValueError, TypeError):
        return {"success": False, "confirmation": "I need a volume between 0 and 100, sir."}
    cmd = set_volume_cmd(level)
    if cmd is None:
        return _unsupported("volume control")
    ok, err = await _run(cmd)
    if ok:
        return {"success": True, "confirmation": f"Volume set to {level} percent, sir."}
    log.warning(f"set_volume failed: {err}")
    return _unsupported("volume control")


async def media(action: str) -> dict:
    action = action.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {"pause": "play_pause", "play": "play_pause", "playpause": "play_pause",
               "skip": "next", "prev": "previous", "back": "previous"}
    action = aliases.get(action, action)
    cmd = media_cmd(action)
    if cmd is None:
        if action not in MEDIA_ACTIONS:
            return {"success": False, "confirmation": "I can play or pause, skip ahead, or go back, sir."}
        return _unsupported("media control")
    ok, err = await _run(cmd)
    if ok:
        spoken = {"play_pause": "Toggled playback", "next": "Skipped ahead", "previous": "Went back a track"}[action]
        return {"success": True, "confirmation": f"{spoken}, sir."}
    log.warning(f"media '{action}' failed: {err}")
    return _unsupported("media control")


async def lock_screen() -> dict:
    cmd = lock_screen_cmd()
    ok, err = await _run(cmd)
    if ok:
        return {"success": True, "confirmation": "Locking up, sir. Back soon, I hope."}
    log.warning(f"lock_screen failed: {err}")
    return _unsupported("locking the screen")


async def clipboard_copy(text: str) -> dict:
    if not text:
        return {"success": False, "confirmation": "There's nothing to copy, sir."}
    cmd = clipboard_copy_cmd()
    ok, err = await _run(cmd, input_text=text)
    if not ok:
        fallback = clipboard_copy_fallback_cmd()
        if fallback:
            ok, err = await _run(fallback, input_text=text)
    if ok:
        return {"success": True, "confirmation": "Copied to your clipboard, sir."}
    log.warning(f"clipboard_copy failed: {err}")
    return _unsupported("the clipboard")


async def take_screenshot() -> dict:
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    out = SCREENSHOTS_DIR / f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    cmd = screenshot_cmd(str(out))
    if cmd is None:
        return _unsupported("screenshots")
    ok, err = await _run(cmd, timeout=20)
    if not ok:
        fallback = screenshot_fallback_cmd(str(out))
        if fallback:
            ok, err = await _run(fallback, timeout=20)
    if ok and out.exists():
        return {"success": True, "confirmation": f"Screenshot saved as {out.name}, sir.", "path": str(out)}
    log.warning(f"take_screenshot failed: {err}")
    return _unsupported("screenshots")


async def open_terminal(command: str = "") -> dict:
    """Non-mac terminal launcher; macOS uses the AppleScript path in actions.py."""
    cmd = terminal_cmd(command)
    if cmd is None:
        return _unsupported("opening a terminal")
    ok, err = await _run(cmd)
    if not ok:
        for fallback in terminal_fallback_cmds(command):
            ok, err = await _run(fallback)
            if ok:
                break
    if ok:
        return {"success": True, "confirmation": "Terminal is open, sir."}
    log.warning(f"open_terminal failed: {err}")
    return _unsupported("opening a terminal")
