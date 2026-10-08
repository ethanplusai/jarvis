"""
JARVIS Action Executor — AppleScript-based system actions.

Execute actions IMMEDIATELY, before generating any LLM response.
Each function returns {"success": bool, "confirmation": str}.
"""

import asyncio
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("jarvis.actions")

async def _mark_terminal_as_jarvis(revert_after: float = 5.0):
    """Temporarily set the front Terminal window to Ocean theme, then revert.

    Shows the user JARVIS is active in that terminal. Reverts after revert_after seconds.
    """
    # Save the current profile, switch to Ocean, then revert
    script_save = (
        'tell application "Terminal"\n'
        '    return name of current settings of front window\n'
        'end tell'
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script_save,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        original_profile = stdout.decode().strip()

        # Switch to Ocean
        script_set = (
            'tell application "Terminal"\n'
            '    set current settings of front window to settings set "Ocean"\n'
            'end tell'
        )
        proc2 = await asyncio.create_subprocess_exec(
            "osascript", "-e", script_set,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc2.communicate()

        # Schedule revert
        if original_profile and original_profile != "Ocean":
            asyncio.get_event_loop().call_later(
                revert_after,
                lambda: asyncio.ensure_future(_revert_terminal_theme(original_profile))
            )
    except Exception:
        pass


async def _revert_terminal_theme(profile_name: str):
    """Revert a Terminal window back to its original profile."""
    escaped = applescript_escape(profile_name)
    script = (
        'tell application "Terminal"\n'
        f'    set current settings of front window to settings set "{escaped}"\n'
        'end tell'
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
    except Exception:
        pass


def applescript_escape(s: str) -> str:
    """Escape a string for safe embedding in an AppleScript double-quoted string."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", " ")


async def open_terminal_at(path: str, command: str = "") -> dict:
    """A terminal in `path`, optionally running `command` there.

    The directory travels separately from the command so that each platform
    can hand it over without a shell parsing it: quoted into a `cd` for
    Terminal.app, as the working directory of a new console on Windows."""
    if sys.platform == "win32":
        return _open_console_windows(path, command)
    script = f"cd {shlex.quote(path)}"
    if command:
        script += f" && {command}"
    return await open_terminal(script)


async def open_terminal(command: str = "") -> dict:
    """Open Terminal.app and optionally run a command. Marks it blue for JARVIS."""
    if command:
        escaped = applescript_escape(command)
        script = (
            'tell application "Terminal"\n'
            "    activate\n"
            f'    do script "{escaped}"\n'
            "end tell"
        )
    else:
        script = (
            'tell application "Terminal"\n'
            "    activate\n"
            "end tell"
        )
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    success = proc.returncode == 0
    if not success:
        log.error(f"open_terminal failed: {stderr.decode()}")
    else:
        await _mark_terminal_as_jarvis()
    return {
        "success": success,
        "confirmation": "Terminal is open, sir." if success else "I had trouble opening Terminal, sir.",
    }


async def open_browser(url: str, browser: str = "chrome") -> dict:
    """Open URL in user's browser (Chrome or Firefox).

    The URL goes through `applescript_escape` and nothing else. A hand-rolled
    `.replace('"', ...)` lived here and escaped the quote but not the
    BACKSLASH, which is the half that matters: AppleScript reads `\\\\` as one
    literal backslash, so a URL ending `x\\"` closes the string literal and
    everything after it is code — and `do shell script` is in that language.
    The URL arrives from a model, out of speech, possibly echoing a page or a
    README, so this is a straight line from attacker text to a shell.
    `tests/test_applescript_url_injection.py` runs the payload.
    """
    if sys.platform == "win32":
        return _open_browser_windows(url, browser)

    escaped_url = applescript_escape(url)

    if browser.lower() == "firefox":
        app_name = "Firefox"
        script = (
            'tell application "Firefox"\n'
            "    activate\n"
            f'    open location "{escaped_url}"\n'
            "end tell"
        )
    else:
        app_name = "Chrome"
        script = (
            'tell application "Google Chrome"\n'
            "    activate\n"
            f'    open location "{escaped_url}"\n'
            "end tell"
        )

    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    success = proc.returncode == 0
    if not success:
        log.error(f"open_browser ({app_name}) failed: {stderr.decode()}")
    return {
        "success": success,
        "confirmation": f"Pulled that up in {app_name}, sir." if success else f"{app_name} ran into a problem, sir.",
    }


# Keep backward compat
async def open_chrome(url: str) -> dict:
    return await open_browser(url, "chrome")


async def get_chrome_tab_info() -> dict:
    """Read the current Chrome tab's title and URL via AppleScript."""
    if sys.platform == "win32":
        return {}           # no scripting bridge into Chrome on Windows
    script = (
        'tell application "Google Chrome"\n'
        "    set tabTitle to title of active tab of front window\n"
        "    set tabURL to URL of active tab of front window\n"
        '    return tabTitle & "|" & tabURL\n'
        "end tell"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            result = stdout.decode().strip()
            parts = result.split("|", 1)
            if len(parts) == 2:
                return {"title": parts[0], "url": parts[1]}
        return {}
    except Exception as e:
        log.warning(f"get_chrome_tab_info failed: {e}")
        return {}


# --- Opening code where the user actually reads it -----------------------
#
# The user's words: "maybe he should be able to open code files in VS Code or
# text editor." VS Code first when it is installed, the system default
# otherwise, so this still works on a Mac that has never had it.
#
# No AppleScript and no shell: `open` is exec'd with the path as its own argv
# entry, so a filename cannot be quoted out of a script the way it can out of
# an AppleScript string literal. Containment and the sensitive-file wall are
# the CALLER's job and have already run by the time this is reached — see
# server.tool_open_in_editor.

VSCODE_APP = "/Applications/Visual Studio Code.app"


def _vscode_command(path: str) -> list[str] | None:
    """The argv that opens `path` in VS Code, or None if it is not installed."""
    binary = shutil.which("code")
    if binary and sys.platform == "win32":
        # `code` on Windows is `bin\code.cmd`, a batch file, and cmd.exe
        # re-parses its arguments: a legal filename like `a&calc.ts` would
        # run `calc`. The executable it wraps takes a path as plain argv.
        exe = Path(binary).resolve().parent.parent / "Code.exe"
        return [str(exe), str(path)] if exe.is_file() else None
    if binary:
        return [binary, str(path)]
    if os.path.isdir(VSCODE_APP):
        return ["open", "-a", VSCODE_APP, str(path)]
    return None


async def open_in_editor(path: str) -> dict:
    """Open a file or directory in VS Code, else in the system default."""
    argv = _vscode_command(path)
    editor = "VS Code"
    if argv is None and sys.platform == "win32":
        # Never os.startfile: on Windows "open with the default app" RUNS an
        # .exe, .bat or .js. Notepad and Explorer only ever display.
        argv = (["explorer.exe", str(path)] if os.path.isdir(path)
                else ["notepad.exe", str(path)])
        editor = "File Explorer" if os.path.isdir(path) else "Notepad"
        try:
            subprocess.Popen(argv, close_fds=True)
        except OSError as e:
            log.error(f"open_in_editor could not launch: {e}")
            return {"success": False, "editor": editor,
                    "confirmation": "I couldn't open an editor, sir."}
        return {"success": True, "editor": editor,
                "confirmation": f"Opened that in {editor}, sir."}
    if argv is None:
        argv = ["open", str(path)]
        editor = "your editor"

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        success = proc.returncode == 0
    except OSError as e:
        log.error(f"open_in_editor could not launch: {e}")
        return {"success": False, "editor": editor,
                "confirmation": "I couldn't open an editor, sir."}

    if not success:
        log.error(f"open_in_editor failed: {stderr.decode(errors='replace')}")
    return {
        "success": success,
        "editor": editor,
        "confirmation": f"Opened that in {editor}, sir." if success
        else f"{editor} wouldn't open that, sir.",
    }


# --- Windows ---------------------------------------------------------------
#
# The directory becomes the new console's working directory, never part of a
# command line, so no path is ever quoted for cmd.exe. The command itself has
# been through `builds.command_problem`, which permits no shell metacharacter.

def _open_console_windows(path: str, command: str = "") -> dict:
    if not os.path.isdir(path):
        return {"success": False,
                "confirmation": "That folder isn't there any more, sir."}
    argv = ["cmd.exe", "/k", command] if command else ["cmd.exe"]
    try:
        subprocess.Popen(argv, cwd=path,
                         creationflags=subprocess.CREATE_NEW_CONSOLE,
                         close_fds=True)
    except OSError as e:
        log.error(f"open_terminal failed: {e}")
        return {"success": False,
                "confirmation": "I had trouble opening a terminal, sir."}
    return {"success": True, "confirmation": "Terminal is open, sir."}


def _windows_browser(name: str) -> tuple[str, str] | None:
    """(display name, exe) for the browser asked for, else Edge, else None."""
    env = os.environ.get
    roots = [env("ProgramFiles", ""), env("ProgramFiles(x86)", ""),
             env("LOCALAPPDATA", "")]
    known = {
        "chrome": ("Chrome", r"Google\Chrome\Application\chrome.exe"),
        "firefox": ("Firefox", os.path.join("Mozilla Firefox", "firefox.exe")),
        "edge": ("Edge", r"Microsoft\Edge\Application\msedge.exe"),
    }
    for key in dict.fromkeys([name.lower(), "edge"]):
        if key not in known:
            continue
        label, rel = known[key]
        for root in roots:
            exe = os.path.join(root, rel)
            if root and os.path.isfile(exe):
                return label, exe
    return None


_URL_SCHEMES = re.compile(r"(?i)(https?|file)://")


def _open_browser_windows(url: str, browser: str) -> dict:
    # The URL is a browser ARGUMENT: one that starts `--` is a browser flag,
    # and one that is not a URL at all could be a program. Only a real
    # http(s)/file URL is passed through.
    if not _URL_SCHEMES.match(url or ""):
        return {"success": False,
                "confirmation": "That isn't an address I can open, sir."}
    found = _windows_browser(browser)
    if found is None:
        return {"success": False,
                "confirmation": "I can't find a browser to open that in, sir."}
    app_name, exe = found
    try:
        subprocess.Popen([exe, url], close_fds=True)
    except OSError as e:
        log.error(f"open_browser ({app_name}) failed: {e}")
        return {"success": False,
                "confirmation": f"{app_name} ran into a problem, sir."}
    return {"success": True,
            "confirmation": f"Pulled that up in {app_name}, sir."}


def _generate_project_name(prompt: str) -> str:
    """Generate a kebab-case project folder name from the prompt."""
    # First: check for a quoted name like "tiktok-analytics-dashboard"
    quoted = re.search(r'"([^"]+)"', prompt)
    if quoted:
        name = quoted.group(1).strip()
        # Already kebab-case or close to it
        name = re.sub(r"[^a-zA-Z0-9\s-]", "", name).strip()
        if name:
            return re.sub(r"[\s]+", "-", name.lower())

    # Second: check for "called X" or "named X" pattern
    called = re.search(r'(?:called|named)\s+(\S+(?:[-_]\S+)*)', prompt, re.IGNORECASE)
    if called:
        name = re.sub(r"[^a-zA-Z0-9-]", "", called.group(1))
        if len(name) > 3:
            return name.lower()

    # Fallback: extract meaningful words
    words = re.sub(r"[^a-zA-Z0-9\s]", "", prompt.lower()).split()
    skip = {"a", "the", "an", "me", "build", "create", "make", "for", "with", "and",
            "to", "of", "i", "want", "need", "new", "project", "directory", "called",
            "on", "desktop", "that", "application", "app", "full", "stack", "simple",
            "web", "page", "site", "named"}
    meaningful = [w for w in words if w not in skip and len(w) > 2][:4]
    return "-".join(meaningful) if meaningful else "jarvis-project"
