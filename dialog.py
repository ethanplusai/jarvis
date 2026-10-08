"""Press one key in the terminal a Claude Code session is running in.

This is the most dangerous module in the project: it sends a synthetic
keystroke to a window on the user's machine. Aimed wrong, it types into
whatever the user is actually working in. Every decision here exists to make
that impossible, so read the three of them before changing anything.

**1. The target is found by tty, never by focus.** A session's pid maps to a
tty (`ps -o tty=`), and Terminal.app publishes the tty of every tab over
AppleScript. That gives an exact pid -> tty -> tab identity with no guessing.
If no Terminal tab owns that tty we return `not_found` and press NOTHING.
There is deliberately no "send it to the front window" fallback: on this
developer's machine the sessions live in Orcha.app, an Electron host whose
tabs AppleScript cannot address, and its ttys do not intersect Terminal's at
all — so a focus fallback would type into an unrelated window every single
time. `not_found` is the common case, and it is the correct one.

**2. The vocabulary is closed.** Return, Escape and a single digit 1-9. That
is the whole set, and it is a safety boundary rather than a convenience:
anything else — free text, an empty string, a multi-character string, a shell
fragment — is refused before any AppleScript is composed, let alone run. This
module never types text.

**3. Nothing here raises.** Every path returns an outcome string, so the
caller can always say something useful and always has something to audit.

Terminal.app only. iTerm2 exposes `tty` per session in much the same shape and
would slot in at `find_terminal_tab`, but it is not installed here, so it is
not claimed and not built. TIOCSTI — the host-independent way to do this — is
refused by macOS 26.2 with PermissionError even on a pty the process owns;
do not go looking for it again.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass

log = logging.getLogger("jarvis.dialog")

# --- outcomes ---------------------------------------------------------------
SENT = "sent"
NO_TTY = "no_tty"           # the pid is dead, or has no controlling terminal
NOT_FOUND = "not_found"     # no Terminal.app tab owns that tty (another host)
NOT_PERMITTED = "not_permitted"   # macOS Accessibility/automation refused us
FAILED = "failed"           # osascript died, timed out, or said something odd
BAD_KEY = "bad_key"         # defensive: a key outside the closed vocabulary

# How long an osascript may run before we stop waiting and kill it. A hung
# osascript (a modal sheet on Terminal, a permission dialog nobody answers)
# must not wedge the caller, which is a voice turn with a person waiting on it.
LOOKUP_TIMEOUT = 10.0
SEND_TIMEOUT = 20.0

# How long `ps` and `pgrep` may take. Both are local, answer in milliseconds,
# and are only ever consulted to decide whether to do nothing — so a long
# ceiling buys nothing and costs a great deal. server.py's
# `_tty_for_session_or_explain` is still SYNCHRONOUS and calls `tty_for_pid`
# once per pid on the voice loop; at the old 5s ceiling a five-process
# session was up to twenty-five seconds of frozen microphone. This bounds
# that until that caller can be made async (see `tty_for_pid_async`).
_PS_TIMEOUT = 1.0

# The closed vocabulary. `key code` numbers rather than `keystroke return`,
# because a key code is unambiguous and cannot be reinterpreted as text.
_RETURN_KEY_CODE = 36
_ESCAPE_KEY_CODE = 53

_ALIASES = {
    "enter": "return",
    "return": "return",
    "yes": "return",       # "yes" answers a permission prompt with Return
    "y": "return",
    "escape": "escape",
    "esc": "escape",
    "cancel": "escape",
    "no": "escape",
    "n": "escape",
}

# Substrings macOS uses when it is Accessibility/automation refusing us, not
# a bug in the script. Each is distinctive enough that it cannot match the
# text of an ordinary AppleScript error.
_PERMISSION_MARKERS = (
    "-1743",                              # not authorized to send Apple events
    "-25211",                             # osascript is not allowed assistive access
    "not allowed assistive access",
    "not authorized to send apple events",
    "is not allowed to send keystrokes",
    "assistive access",
)


@dataclass(frozen=True)
class TerminalTab:
    """Enough to re-select one Terminal.app tab, plus the tty that identified
    it — kept so the send script can re-check the identity at press time."""
    window_id: int
    tab_index: int
    tty: str


def normalize_key(key) -> str | None:
    """The closed vocabulary, or None. None means REFUSE — never interpret.

    Returns "return", "escape", or a single digit "1".."9". Anything else,
    including free text that merely starts with an accepted word, is None.
    """
    if not isinstance(key, str):
        return None
    k = key.strip().lower()
    if k in _ALIASES:
        return _ALIASES[k]
    if len(k) == 1 and k in "123456789":
        return k
    return None


def spoken_key(normalized: str) -> str:
    """How the read-back names the key. Must match what actually gets sent."""
    return {"return": "Return", "escape": "Escape"}.get(normalized, normalized)


def normalize_tty(tty: str | None) -> str | None:
    """`ttys006` and `/dev/ttys006` are the same device; Terminal reports the
    long form and `ps` the short one. Everything downstream compares the long
    form so the match can be exact rather than a suffix test — `ttys1` must
    never match `ttys11`."""
    if not tty:
        return None
    t = tty.strip()
    if not t or t == "??":
        return None
    if not t.startswith("/dev/"):
        t = "/dev/" + t
    # A device path and nothing else. Anything stranger is not a tty we will
    # act on, and refusing here keeps unvetted text out of the AppleScript.
    return t if re.fullmatch(r"/dev/tty[a-zA-Z0-9]+", t) else None


def tty_for_pid(pid) -> str | None:
    """`/dev/ttysNNN` for a live pid with a controlling terminal, else None.

    None covers all three of: a dead pid, a pid `ps` reports as `??` (a
    session started without a terminal — the `sdk-cli` entrypoint on this
    machine is one), and anything unparseable.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        import subprocess
        out = subprocess.run(["ps", "-o", "tty=", "-p", str(pid)],
                             capture_output=True, text=True,
                             timeout=_PS_TIMEOUT)
    except Exception as e:
        log.warning(f"tty lookup for pid {pid} failed: {e}")
        return None
    if out.returncode != 0:
        return None                      # ps exits non-zero for a dead pid
    return normalize_tty(out.stdout.strip())


async def tty_for_pid_async(pid) -> str | None:
    """`tty_for_pid` off the event loop.

    Identical answer; the `ps` call just happens on a worker thread. Anything
    async must use this one — a blocking subprocess on the voice loop is a
    frozen microphone, and `ps` is exactly where that used to happen.

    The synchronous `tty_for_pid` stays as the function tests patch (this
    one delegates to it, so a patch covers both) and for any caller that is
    genuinely synchronous. Nothing on the voice path calls it directly any
    more.
    """
    return await asyncio.to_thread(tty_for_pid, pid)


def _terminal_is_running() -> bool:
    """True only if Terminal.app already has a process.

    Checked with pgrep and NOT with AppleScript, because `tell application
    "Terminal"` LAUNCHES it. A read-only "is my session in a Terminal tab?"
    lookup must never open an application on the user's screen. Anything
    unexpected answers False, which costs a `not_found` and opens nothing.
    """
    if shutil.which("pgrep") is None:
        return False
    try:
        import subprocess
        return subprocess.run(["pgrep", "-x", "Terminal"],
                              capture_output=True,
                              timeout=_PS_TIMEOUT).returncode == 0
    except Exception:
        return False


async def _osascript(script: str, timeout: float) -> tuple[int, str, str]:
    """Run one AppleScript. Returns (returncode, stdout, stderr).

    A timeout kills the child and comes back as returncode -1 so the caller
    sees a failure rather than hanging on a script that will never return.
    """
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        log.warning(f"osascript timed out after {timeout}s")
        return -1, "", "timeout"
    return (proc.returncode or 0,
            stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"))


def _is_permission_error(stderr: str) -> bool:
    low = stderr.lower()
    return any(m in low for m in _PERMISSION_MARKERS)


_ENUMERATE_SCRIPT = '''
tell application "Terminal"
    set out to ""
    repeat with w in windows
        set wid to (id of w) as text
        set n to (count of tabs of w)
        repeat with i from 1 to n
            set tt to ""
            try
                set tt to (tty of tab i of w) as text
            end try
            set out to out & wid & ":" & (i as text) & ":" & tt & linefeed
        end repeat
    end repeat
    return out
end tell
'''


async def find_terminal_tab(tty: str | None) -> TerminalTab | None:
    """The Terminal.app tab whose tty is EXACTLY `tty`, or None.

    None is a normal, expected answer — it is what a session hosted by any
    application other than Terminal.app returns, and it is what stops this
    module from acting. It is never upgraded into a best guess.
    """
    want = normalize_tty(tty)
    if want is None:
        return None
    # `to_thread`, not a direct call: `pgrep` is a blocking subprocess and
    # this runs on the voice loop.
    if not await asyncio.to_thread(_terminal_is_running):
        return None
    code, stdout, stderr = await _osascript(_ENUMERATE_SCRIPT, LOOKUP_TIMEOUT)
    if code != 0:
        log.warning(f"Terminal tab enumeration failed ({code}): {stderr.strip()}")
        return None
    for line in stdout.splitlines():
        parts = line.strip().split(":")
        if len(parts) != 3:
            continue
        wid, idx, tab_tty = parts
        if normalize_tty(tab_tty) != want:
            continue
        try:
            return TerminalTab(window_id=int(wid), tab_index=int(idx), tty=want)
        except ValueError:
            continue
    return None


def _send_script(tab: TerminalTab, normalized: str) -> str:
    """The one script that activates, presses, and hands focus back.

    Three things are load-bearing:

    * It re-reads the tab's tty and ABORTS if it no longer matches. Tabs can
      close or be reordered between the lookup and the press; without this,
      a window id and index that pointed at the right session a moment ago
      could point at a different one now.
    * The frontmost application is captured BEFORE Terminal is activated and
      restored after, so a keypress the user asked for does not leave them
      staring at a terminal they were not using.
    * Only `key code` for Return/Escape, or `keystroke` of a single literal
      digit. `normalized` has already been through `normalize_key`, so the
      only values that can reach the interpolation are "return", "escape",
      or one character of "123456789" — nothing user-authored is ever
      substituted into this script.
    """
    if normalized == "return":
        press = f"key code {_RETURN_KEY_CODE}"
    elif normalized == "escape":
        press = f"key code {_ESCAPE_KEY_CODE}"
    else:
        press = f'keystroke "{normalized}"'
    return f'''
tell application "System Events"
    set priorApp to ""
    try
        set priorApp to name of first application process whose frontmost is true
    end try
end tell
tell application "Terminal"
    set targetWindow to missing value
    repeat with w in windows
        if (id of w) is {tab.window_id} then set targetWindow to w
    end repeat
    if targetWindow is missing value then return "gone"
    if (count of tabs of targetWindow) < {tab.tab_index} then return "gone"
    set targetTab to tab {tab.tab_index} of targetWindow
    set nowTty to ""
    try
        set nowTty to (tty of targetTab) as text
    end try
    if nowTty is not "{tab.tty}" then return "moved"
    activate
    set selected tab of targetWindow to targetTab
    set index of targetWindow to 1
end tell
delay 0.2
tell application "System Events"
    tell process "Terminal" to set frontmost to true
    {press}
end tell
delay 0.1
tell application "System Events"
    if priorApp is not "" and priorApp is not "Terminal" then
        try
            set frontmost of process priorApp to true
        end try
    end if
end tell
return "ok"
'''


async def answer(pid: int, key: str) -> str:
    """Press one key in the terminal that owns `pid`. Never raises.

    Returns `sent`, `no_tty`, `not_found`, `not_permitted`, `failed`, or —
    defensively, for a caller that skipped its own validation — `bad_key`.
    Only `sent` means a keystroke actually left this machine's event queue.
    """
    normalized = normalize_key(key)
    if normalized is None:
        # Before any script is composed: nothing about the rejected key ever
        # reaches AppleScript, so there is nothing to escape and nothing to
        # get wrong.
        log.warning(f"refusing a key outside the vocabulary: {key!r}")
        return BAD_KEY
    try:
        tty = await tty_for_pid_async(pid)
        if tty is None:
            return NO_TTY
        tab = await find_terminal_tab(tty)
        if tab is None:
            # Another application hosts this tty. Press nothing.
            return NOT_FOUND
        code, stdout, stderr = await _osascript(_send_script(tab, normalized),
                                                SEND_TIMEOUT)
        if code != 0:
            if _is_permission_error(stderr):
                log.warning(f"keystroke refused by macOS: {stderr.strip()}")
                return NOT_PERMITTED
            log.warning(f"keystroke script failed ({code}): {stderr.strip()}")
            return FAILED
        result = stdout.strip()
        if result == "ok":
            return SENT
        if result in ("gone", "moved"):
            # The tab closed or the tty moved between the lookup and the
            # press. Nothing was pressed; say so as not_found, which is the
            # same thing from the user's side.
            log.warning(f"tab for {tty} was {result} at press time")
            return NOT_FOUND
        log.warning(f"unexpected keystroke script result: {result!r}")
        return FAILED
    except Exception as e:
        log.warning(f"answering a dialog for pid {pid} failed: {e}", exc_info=True)
        return FAILED
