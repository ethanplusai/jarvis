"""
JARVIS System Control — AppleScript-based macOS system actions.

Every function:
  - Uses AppleScript as the primary method
  - Falls back to subprocess/pyautogui where noted
  - Returns {"success": bool, "confirmation": str}
  - Logs every action to data/jarvis_actions.log (no file contents logged)

Requires:
  - Accessibility access for the process running JARVIS (Terminal / python)
  - Automation access for Chrome, Finder, System Events
  - Microphone already handled by the frontend
"""

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("jarvis.system_control")

_ACTION_LOG = Path(__file__).parent / "data" / "jarvis_actions.log"

_PERM_HINTS = {
    "accessibility": (
        "JARVIS needs Accessibility access. "
        "Open System Settings → Privacy & Security → Accessibility "
        "and enable Terminal (or the app running JARVIS)."
    ),
    "automation": (
        "JARVIS needs Automation access. "
        "Open System Settings → Privacy & Security → Automation "
        "and allow the required app targets."
    ),
    "screen_recording": (
        "JARVIS needs Screen Recording access for screenshots. "
        "Open System Settings → Privacy & Security → Screen Recording "
        "and enable Terminal (or the app running JARVIS)."
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log_action(action: str, result: str) -> None:
    """Append one line to the action log. Never logs file contents or personal data."""
    try:
        _ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(_ACTION_LOG, "a") as f:
            f.write(f"{ts} | {action} | {result}\n")
    except Exception:
        pass


async def _run_script(script: str, timeout: int = 10) -> tuple[bool, str, str]:
    """Execute an AppleScript via osascript.

    Returns (success, stdout_stripped, stderr_stripped).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        ok = proc.returncode == 0
        return ok, stdout.decode().strip(), stderr.decode().strip()
    except asyncio.TimeoutError:
        return False, "", "timeout"
    except Exception as e:
        return False, "", str(e)


def _perm_error(stderr: str) -> str | None:
    """If stderr signals a missing permission, return a helpful hint."""
    s = stderr.lower()
    if "not authorized" in s or "assistive" in s or "accessibility" in s:
        return _PERM_HINTS["accessibility"]
    if "automation" in s or "not allowed" in s:
        return _PERM_HINTS["automation"]
    if "screen recording" in s or "screencapture" in s:
        return _PERM_HINTS["screen_recording"]
    return None


def _result(action: str, success: bool, msg: str, stderr: str = "") -> dict:
    """Build the standard return dict and write the action log."""
    hint = _perm_error(stderr) if not success else ""
    confirmation = msg + (f" ({hint})" if hint else "")
    _log_action(action, "OK" if success else f"FAIL: {stderr[:80]}")
    return {"success": success, "confirmation": confirmation}


# ---------------------------------------------------------------------------
# Browser Control (Google Chrome via AppleScript)
# ---------------------------------------------------------------------------

async def open_new_tab(url: str = "") -> dict:
    """Open a new Chrome tab, optionally navigating to url."""
    safe_url = url.replace('"', "")
    if safe_url:
        script = f'''
tell application "Google Chrome"
    activate
    tell front window to make new tab with properties {{URL:"{safe_url}"}}
end tell'''
    else:
        script = '''
tell application "Google Chrome"
    activate
    tell front window to make new tab
end tell'''
    ok, _, err = await _run_script(script)
    if not ok and "no windows" in err.lower():
        # No window open — open Chrome fresh
        ok2, _, err2 = await _run_script(
            f'tell application "Google Chrome"\n    activate\n    open location "{safe_url or "about:newtab"}"\nend tell'
        )
        ok, err = ok2, err2
    msg = ("Opened a new tab, sir." if not url else f"Navigated to that in a new tab, sir.") if ok \
          else "Couldn't open a new tab, sir."
    return _result("open_new_tab", ok, msg, err)


async def close_chrome_window() -> dict:
    """Close the front Chrome window (all its tabs)."""
    script = '''
tell application "Google Chrome"
    if (count of windows) = 0 then return "NO_WINDOW"
    close front window
    return "OK"
end tell'''
    ok, out, err = await _run_script(script)
    if out == "NO_WINDOW":
        return _result("close_chrome_window", False, "No Chrome window to close, sir.", "")
    msg = "Chrome window closed, sir." if ok else "Couldn't close that window, sir."
    return _result("close_chrome_window", ok, msg, err)


async def browser_back() -> dict:
    """Go back in the front Chrome tab."""
    script = '''
tell application "Google Chrome"
    tell active tab of front window to go back
end tell'''
    ok, _, err = await _run_script(script)
    return _result("browser_back", ok, "Going back, sir." if ok else "Couldn't go back, sir.", err)


async def browser_forward() -> dict:
    """Go forward in the front Chrome tab."""
    script = '''
tell application "Google Chrome"
    tell active tab of front window to go forward
end tell'''
    ok, _, err = await _run_script(script)
    return _result("browser_forward", ok, "Going forward, sir." if ok else "Couldn't go forward, sir.", err)


async def reload_page() -> dict:
    """Reload the front Chrome tab."""
    script = '''
tell application "Google Chrome"
    tell active tab of front window to reload
end tell'''
    ok, _, err = await _run_script(script)
    return _result("reload_page", ok, "Page reloaded, sir." if ok else "Couldn't reload, sir.", err)


async def get_tab_info() -> dict:
    """Return title and URL of the front Chrome tab."""
    script = '''
tell application "Google Chrome"
    if (count of windows) = 0 then return "NO_WINDOW"
    set t to active tab of front window
    return (title of t) & "|" & (URL of t)
end tell'''
    ok, out, err = await _run_script(script)
    if not ok or out == "NO_WINDOW":
        return _result("get_tab_info", False, "No Chrome tab available, sir.", err)
    parts = out.split("|", 1)
    title = parts[0].strip() if parts else ""
    url   = parts[1].strip() if len(parts) > 1 else ""
    _log_action("get_tab_info", f"title={title[:60]}")
    return {"success": True, "confirmation": f"Active tab: {title}", "title": title, "url": url}


# ---------------------------------------------------------------------------
# App / Window Control
# ---------------------------------------------------------------------------

async def switch_to_app(app_name: str) -> dict:
    """Bring an application to the foreground."""
    safe = app_name.replace('"', "")
    script = f'tell application "{safe}" to activate'
    ok, _, err = await _run_script(script)
    msg = f"Switched to {safe}, sir." if ok else f"Couldn't find or switch to {safe}, sir."
    return _result("switch_to_app", ok, msg, err)


async def quit_app(app_name: str) -> dict:
    """Quit an application gracefully (Cmd+Q through System Events)."""
    safe = app_name.replace('"', "")
    script = f'''
tell application "{safe}"
    quit
end tell'''
    ok, _, err = await _run_script(script)
    # Fallback: use System Events keystroke
    if not ok:
        fallback = f'''
tell application "{safe}" to activate
delay 0.3
tell application "System Events"
    keystroke "q" using command down
end tell'''
        ok, _, err = await _run_script(fallback)
    msg = f"Quitting {safe}, sir." if ok else f"Couldn't quit {safe}, sir."
    return _result("quit_app", ok, msg, err)


async def hide_app(app_name: str) -> dict:
    """Hide (Cmd+H) the named application."""
    safe = app_name.replace('"', "")
    script = f'''
tell application "{safe}" to activate
delay 0.2
tell application "System Events"
    keystroke "h" using command down
end tell'''
    ok, _, err = await _run_script(script)
    msg = f"Hidden {safe}, sir." if ok else f"Couldn't hide {safe}, sir."
    return _result("hide_app", ok, msg, err)


async def minimize_window() -> dict:
    """Minimize the front window of the frontmost app."""
    script = '''
tell application "System Events"
    set frontApp to name of first application process whose frontmost is true
end tell
tell application frontApp
    set miniaturized of front window to true
end tell'''
    ok, _, err = await _run_script(script)
    # Fallback: Cmd+M keystroke
    if not ok:
        script2 = '''
tell application "System Events"
    keystroke "m" using command down
end tell'''
        ok, _, err = await _run_script(script2)
    return _result("minimize_window", ok, "Minimised, sir." if ok else "Couldn't minimise that window, sir.", err)


async def maximize_window() -> dict:
    """Zoom (maximise) the front window via Cmd+Ctrl+F (full screen) or green button."""
    script = '''
tell application "System Events"
    keystroke "f" using {command down, control down}
end tell'''
    ok, _, err = await _run_script(script)
    return _result("maximize_window", ok, "Maximised, sir." if ok else "Couldn't maximise, sir.", err)


async def move_window_to_half(side: str) -> dict:
    """Snap the front window to the left or right half of the screen via Rectangle shortcuts.

    Rectangle's default shortcuts:
      Left half:  Ctrl+Opt+Left
      Right half: Ctrl+Opt+Right
    Falls back to a direct window bounds calculation if Rectangle isn't running.
    """
    side = side.lower().strip()
    if side not in ("left", "right"):
        return _result("move_window", False, f"I don't recognise '{side}' — say left or right, sir.", "")

    key_code = "123" if side == "left" else "124"  # left/right arrow key codes
    # Try Rectangle shortcut first (most reliable)
    script = f'''
tell application "System Events"
    key code {key_code} using {{control down, option down}}
end tell'''
    ok, _, err = await _run_script(script)

    if not ok:
        # Fallback: use built-in macOS window tiling (macOS 15+)
        tile_side = "left" if side == "left" else "right"
        script2 = f'''
tell application "System Events"
    set frontApp to name of first application process whose frontmost is true
end tell
tell application frontApp
    set bounds of front window to tile to {tile_side}
end tell'''
        ok, _, err = await _run_script(script2)

    msg = f"Moved window to the {side} half, sir." if ok else f"Couldn't snap window to {side}, sir. Rectangle may not be installed."
    return _result("move_window", ok, msg, err)


# ---------------------------------------------------------------------------
# Keyboard Shortcuts (via System Events)
# ---------------------------------------------------------------------------

_KBD_ACTIONS: dict[str, tuple[str, str, str]] = {
    # action_name: (key, modifier_string, friendly_name)
    "copy":       ("c", "command down", "Copied"),
    "paste":      ("v", "command down", "Pasted"),
    "undo":       ("z", "command down", "Undone"),
    "redo":       ("z", "{command down, shift down}", "Redone"),
    "select_all": ("a", "command down", "Selected all"),
    "save":       ("s", "command down", "Saved"),
    "new_tab":    ("t", "command down", "New tab opened"),
    "new_window": ("n", "command down", "New window opened"),
    "new_file":   ("n", "command down", "New file opened"),
}


async def _send_keystroke(key: str, modifiers: str, action_name: str, friendly: str) -> dict:
    script = f'''
tell application "System Events"
    keystroke "{key}" using {modifiers}
end tell'''
    ok, _, err = await _run_script(script)
    msg = f"{friendly}, sir." if ok else f"Keystroke failed for {action_name}, sir."
    return _result(action_name, ok, msg, err)


async def copy_selection() -> dict:
    return await _send_keystroke("c", "command down", "copy", "Copied")


async def paste_clipboard() -> dict:
    return await _send_keystroke("v", "command down", "paste", "Pasted")


async def undo_last() -> dict:
    return await _send_keystroke("z", "command down", "undo", "Undone")


async def redo_last() -> dict:
    script = '''
tell application "System Events"
    keystroke "z" using {command down, shift down}
end tell'''
    ok, _, err = await _run_script(script)
    return _result("redo", ok, "Redone, sir." if ok else "Redo failed, sir.", err)


async def select_all() -> dict:
    return await _send_keystroke("a", "command down", "select_all", "Selected all")


async def save_document() -> dict:
    return await _send_keystroke("s", "command down", "save", "Saved")


async def take_screenshot() -> dict:
    """Capture the full screen to ~/Desktop using screencapture."""
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = Path.home() / "Desktop" / f"screenshot_{ts}.png"
    try:
        proc = await asyncio.create_subprocess_exec(
            "screencapture", "-x", str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err_bytes = await asyncio.wait_for(proc.communicate(), timeout=15)
        ok = proc.returncode == 0 and path.exists()
        err = err_bytes.decode().strip()
        msg = f"Screenshot saved to Desktop as {path.name}, sir." if ok \
              else "Screenshot failed, sir. Screen Recording permission may be needed."
        return _result("screenshot", ok, msg, err)
    except asyncio.TimeoutError:
        return _result("screenshot", False, "Screenshot timed out, sir.", "timeout")
    except Exception as e:
        return _result("screenshot", False, "Screenshot failed, sir.", str(e))


# ---------------------------------------------------------------------------
# Scroll
# ---------------------------------------------------------------------------

async def scroll(direction: str, amount: int = 3) -> dict:
    """Scroll the frontmost window up or down.

    Uses Page Up / Page Down key codes via System Events.
    direction: 'up' | 'down'
    """
    direction = direction.lower().strip()
    if direction not in ("up", "down"):
        return _result("scroll", False, f"Say 'up' or 'down', sir.", "")

    # Page Down = key code 121, Page Up = key code 116
    key_code = "121" if direction == "down" else "116"
    script = f'''
tell application "System Events"
    repeat {min(amount, 5)} times
        key code {key_code}
    end repeat
end tell'''
    ok, _, err = await _run_script(script)
    return _result("scroll", ok, f"Scrolled {direction}, sir." if ok else f"Couldn't scroll, sir.", err)


# ---------------------------------------------------------------------------
# Volume & Audio
# ---------------------------------------------------------------------------

async def set_volume(level: int) -> dict:
    """Set system output volume to 0–100."""
    level = max(0, min(100, int(level)))
    script = f"set volume output volume {level}"
    ok, _, err = await _run_script(script)
    msg = f"Volume set to {level}, sir." if ok else "Couldn't set volume, sir."
    return _result("set_volume", ok, msg, err)


async def mute_audio() -> dict:
    """Mute system audio."""
    ok, _, err = await _run_script("set volume with output muted")
    return _result("mute", ok, "Muted, sir." if ok else "Couldn't mute, sir.", err)


async def unmute_audio() -> dict:
    """Unmute system audio."""
    ok, _, err = await _run_script("set volume without output muted")
    return _result("unmute", ok, "Unmuted, sir." if ok else "Couldn't unmute, sir.", err)


async def get_volume() -> dict:
    """Return current volume level and mute state."""
    ok, out, err = await _run_script("get volume settings")
    if not ok:
        return _result("get_volume", False, "Couldn't read volume, sir.", err)
    # out looks like: "output volume:57, input volume:75, alert volume:100, output muted:false"
    level = "unknown"
    muted = False
    for part in out.split(","):
        p = part.strip()
        if p.startswith("output volume:"):
            level = p.split(":")[1].strip()
        if p.startswith("output muted:"):
            muted = p.split(":")[1].strip().lower() == "true"
    mute_str = " (muted)" if muted else ""
    _log_action("get_volume", f"level={level} muted={muted}")
    return {"success": True, "confirmation": f"Volume is at {level}{mute_str}, sir.",
            "level": level, "muted": muted}


# ---------------------------------------------------------------------------
# Finder / File System
# ---------------------------------------------------------------------------

async def open_folder(path: str) -> dict:
    """Open a folder in Finder."""
    safe = path.replace('"', "").replace("\\", "/")
    # Expand ~ manually since AppleScript doesn't
    if safe.startswith("~"):
        safe = str(Path.home()) + safe[1:]
    if not Path(safe).exists():
        return _result("open_folder", False, f"That path doesn't appear to exist, sir.", "")
    script = f'''
tell application "Finder"
    activate
    open folder POSIX file "{safe}"
end tell'''
    ok, _, err = await _run_script(script)
    return _result("open_folder", ok,
                   "Opened in Finder, sir." if ok else "Couldn't open that folder, sir.", err)


async def trash_file(path: str) -> dict:
    """Move a file to the Trash (NOT permanent delete)."""
    safe = path.replace('"', "").replace("\\", "/")
    if safe.startswith("~"):
        safe = str(Path.home()) + safe[1:]
    p = Path(safe)
    if not p.exists():
        return _result("trash_file", False, f"That file doesn't appear to exist, sir.", "")
    script = f'''
tell application "Finder"
    move POSIX file "{safe}" to trash
end tell'''
    ok, _, err = await _run_script(script)
    return _result("trash_file", ok,
                   f"Moved {p.name} to Trash, sir." if ok else "Couldn't move that to Trash, sir.", err)


async def reveal_in_finder(path: str) -> dict:
    """Reveal a file or folder in Finder (select it)."""
    safe = path.replace('"', "").replace("\\", "/")
    if safe.startswith("~"):
        safe = str(Path.home()) + safe[1:]
    if not Path(safe).exists():
        return _result("reveal_file", False, "That path doesn't appear to exist, sir.", "")
    script = f'''
tell application "Finder"
    activate
    reveal POSIX file "{safe}"
end tell'''
    ok, _, err = await _run_script(script)
    return _result("reveal_file", ok,
                   "Revealed in Finder, sir." if ok else "Couldn't reveal that file, sir.", err)


# ---------------------------------------------------------------------------
# Dispatch table — maps action tag string to coroutine
# ---------------------------------------------------------------------------

async def dispatch(action: str, target: str) -> dict:
    """Route an action tag to the right handler.

    action: lowercase action name (e.g. "open_tab", "set_volume")
    target: argument string from the tag (may be empty)
    """
    t = target.strip()

    if action == "open_tab":
        return await open_new_tab(t)
    elif action == "close_window":
        return await close_chrome_window()
    elif action == "browser_back":
        return await browser_back()
    elif action == "browser_forward":
        return await browser_forward()
    elif action == "reload":
        return await reload_page()
    elif action == "get_tab":
        return await get_tab_info()
    elif action == "switch_app":
        return await switch_to_app(t) if t else {"success": False, "confirmation": "Which app, sir?"}
    elif action == "quit_app":
        return await quit_app(t) if t else {"success": False, "confirmation": "Which app shall I quit, sir?"}
    elif action == "hide_app":
        return await hide_app(t) if t else {"success": False, "confirmation": "Which app shall I hide, sir?"}
    elif action == "minimize_window":
        return await minimize_window()
    elif action == "maximize_window":
        return await maximize_window()
    elif action == "move_window":
        return await move_window_to_half(t or "left")
    elif action == "copy":
        return await copy_selection()
    elif action == "paste":
        return await paste_clipboard()
    elif action == "undo":
        return await undo_last()
    elif action == "redo":
        return await redo_last()
    elif action == "select_all":
        return await select_all()
    elif action == "save":
        return await save_document()
    elif action == "screenshot":
        return await take_screenshot()
    elif action == "scroll":
        return await scroll(t or "down")
    elif action == "set_volume":
        try:
            return await set_volume(int(t))
        except (ValueError, TypeError):
            return {"success": False, "confirmation": "Please give a volume level from 0 to 100, sir."}
    elif action == "mute":
        return await mute_audio()
    elif action == "unmute":
        return await unmute_audio()
    elif action == "get_volume":
        return await get_volume()
    elif action == "open_folder":
        return await open_folder(t) if t else {"success": False, "confirmation": "Which folder, sir?"}
    elif action == "trash_file":
        return await trash_file(t) if t else {"success": False, "confirmation": "Which file, sir?"}
    elif action == "reveal_file":
        return await reveal_in_finder(t) if t else {"success": False, "confirmation": "Which file, sir?"}
    else:
        return {"success": False, "confirmation": f"Unknown action '{action}', sir."}
