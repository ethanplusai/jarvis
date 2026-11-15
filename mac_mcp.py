#!/usr/bin/env python3
"""
mac_mcp.py — local MCP server (stdio, JSON-RPC) that lets JARVIS operate the Mac.

Design rules, so he does things accurately instead of guessing:
  * Dedicated actions talk to each app's own scripting interface, not the mouse.
  * Every action VERIFIES and reports what actually happened (what is playing
    now, the note that now exists, the volume it is set to, what the window
    looks like after a click) — never "done" on faith.
  * Anything that leaves the Mac (a text, an email) is a two-step: the first
    call returns a preview; only a second call with confirmed=true sends, and
    JARVIS must have heard the user say yes in between.
  * Destructive scripting (delete, empty trash, shell commands, passwords) is
    refused outright in run_applescript.
  * User text is passed to AppleScript as argv, never spliced into the source.

Declared in data/jarvis/connections.json as the "mac" server.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from jarvis_mcp import _forward
from untrusted_text import wrap

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"}


# --------------------------------------------------------------------------- helpers

def osa(script: str, *args: str, timeout: float = 30.0) -> str:
    """Run AppleScript with user values passed as argv (`on run argv`)."""
    r = subprocess.run(["osascript", "-", *args], input=script, capture_output=True,
                       text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "AppleScript failed").strip()[:400])
    return r.stdout.strip()


def app_running(name: str) -> bool:
    try:
        return osa('on run argv\nreturn application (item 1 of argv) is running\nend run', name) == "true"
    except Exception:
        return False


def frontmost_app() -> str:
    try:
        return osa('tell application "System Events" to return name of first application process whose frontmost is true')
    except Exception:
        return "unknown"


def parse_when(s: str) -> datetime:
    """ISO 8601 ('2026-11-14T15:00') from the brain, which knows the time."""
    s = (s or "").strip().replace("Z", "")
    return datetime.fromisoformat(s)


def as_date_script(var: str, dt: datetime) -> str:
    """AppleScript lines that build `var` as a date without locale-dependent parsing."""
    return (f"set {var} to current date\n"
            f"set day of {var} to 1\n"
            f"set year of {var} to {dt.year}\n"
            f"set month of {var} to {dt.month}\n"
            f"set day of {var} to {dt.day}\n"
            f"set time of {var} to {dt.hour * 3600 + dt.minute * 60}\n")


def _refusal(tool: str) -> str | None:
    """JARVIS's acting-tool gate, or None to go ahead.

    The CLI calls this server directly, so server.py never sees these calls;
    `may_act` puts them through the same check (the user asked, and nothing
    foreign was read this turn). Closed when JARVIS can't be asked.
    """
    if not os.getenv("JARVIS_TOOL_URL"):
        return "Refused: JARVIS isn't there to check this is safe to do."
    ok, text, _ = _forward("may_act", {"tool": tool})
    return None if ok else text


# --------------------------------------------------------------------------- apps

def app(action: str, name: str = "") -> str:
    action = (action or "").lower()
    if action == "list":
        names = osa('tell application "System Events" to return name of every application process whose background only is false')
        return f"Running apps: {names}. In front: {frontmost_app()}."
    name = (name or "").strip()
    if not name or "/" in name:
        return "Which app?"
    if action in ("open", "activate", "switch"):
        r = subprocess.run(["open", "-a", name], capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            found = subprocess.run(["mdfind", f"kMDItemContentType == 'com.apple.application-bundle' && kMDItemDisplayName == '*{name}*'cd"],
                                   capture_output=True, text=True, timeout=10).stdout.split("\n")
            apps = sorted({os.path.basename(p)[:-4] for p in found if p.endswith(".app")})[:6]
            return f"No app called {name}." + (f" Close matches: {', '.join(apps)}." if apps else "")
        time.sleep(0.8)
        return f"{name} is open. In front now: {frontmost_app()}."
    if action in ("quit", "close"):
        if not app_running(name):
            return f"{name} wasn't running."
        osa('on run argv\ntell application (item 1 of argv) to quit\nend run', name, timeout=20)
        time.sleep(1.0)
        return f"{name} quit." if not app_running(name) else f"{name} is still running — it may be asking to save something."
    if action == "hide":
        osa('on run argv\ntell application "System Events" to set visible of process (item 1 of argv) to false\nend run', name)
        return f"{name} hidden."
    return "action must be open, quit, hide or list."


# --------------------------------------------------------------------------- media

def _now_playing(player: str) -> str:
    try:
        out = osa(f'''tell application "{player}"
  if player state is stopped then return "stopped"
  set t to current track
  return (player state as string) & "|" & (name of t) & "|" & (artist of t)
end tell''')
    except Exception as e:
        return f"unknown ({e})"
    if out == "stopped":
        return f"{player}: stopped"
    state, name, artist = (out.split("|") + ["", "", ""])[:3]
    return f"{player}: {state} — “{name}” by {artist}"


def _spotify_uri(query: str, kind: str) -> str | None:
    """Find a Spotify URI for a query via a web search of open.spotify.com links."""
    kinds = {"track": "track", "song": "track", "album": "album", "playlist": "playlist", "artist": "artist"}
    k = kinds.get((kind or "track").lower(), "track")
    for url in (f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(f'site:open.spotify.com/{k} {query}')}",
                f"https://lite.duckduckgo.com/lite/?q={urllib.parse.quote(f'open.spotify.com {k} {query}')}"):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=12) as r:
                html = urllib.parse.unquote(r.read().decode("utf-8", "replace"))
        except Exception:
            continue
        m = re.search(rf"open\.spotify\.com/(?:intl-[a-z]+/)?{k}/([A-Za-z0-9]{{22}})", html)
        if m:
            return f"spotify:{k}:{m.group(1)}"
    return None


def media(action: str, query: str = "", kind: str = "track", player: str = "") -> str:
    action = (action or "").lower()
    player = (player or "").strip().lower()
    p = "Music" if player in ("music", "apple music") else "Spotify"
    if action in ("now_playing", "status"):
        return _now_playing(p)
    if action == "play" and query:
        if p == "Spotify":
            try:
                uri = _spotify_uri(query, kind)
            except Exception as e:
                return f"Couldn't search for {query}: {e}"
            if not uri:
                return f"Couldn't find {kind} “{query}” on Spotify."
            if not app_running("Spotify"):
                subprocess.run(["open", "-a", "Spotify"])
                time.sleep(4)
            if uri.startswith("spotify:track:"):
                osa('on run argv\ntell application "Spotify" to play track (item 1 of argv)\nend run', uri)
            else:   # album / playlist / artist: play its context
                osa('on run argv\ntell application "Spotify" to play track (item 1 of argv) in context (item 1 of argv)\nend run', uri)
            time.sleep(1.5)
            return f"Asked for {kind} “{query}”. Now: {_now_playing('Spotify')}. If that isn't what he wanted, say so."
        out = osa('''on run argv
tell application "Music"
  set r to search playlist "Library" for (item 1 of argv)
  if r is {} then return "none"
  play item 1 of r
end tell
end run''', query)
        if out == "none":
            return f"Nothing matching “{query}” in the Music library."
        time.sleep(1)
        return f"Now: {_now_playing('Music')}."
    cmds = {"play": "play", "resume": "play", "pause": "pause", "toggle": "playpause",
            "next": "next track", "skip": "next track", "previous": "previous track", "back": "previous track"}
    if action in cmds:
        osa(f'tell application "{p}" to {cmds[action]}')
        time.sleep(0.8)
        return f"Now: {_now_playing(p)}."
    return "action must be play (with or without query), pause, toggle, next, previous or now_playing."


# --------------------------------------------------------------------------- system

def system(action: str, value: int | None = None) -> str:
    a = (action or "").lower()
    def vol() -> str:
        return osa("return (output volume of (get volume settings) as string) & \"|\" & (output muted of (get volume settings) as string)")
    if a in ("volume", "set_volume"):
        v = max(0, min(100, int(value if value is not None else 50)))
        osa(f"set volume output volume {v}\nset volume without output muted")
    elif a in ("volume_up", "louder"):
        osa("set volume output volume ((output volume of (get volume settings)) + 10)")
    elif a in ("volume_down", "quieter"):
        osa("set volume output volume ((output volume of (get volume settings)) - 10)")
    elif a == "mute":
        osa("set volume with output muted")
    elif a == "unmute":
        osa("set volume without output muted")
    elif a in ("dark_mode", "light_mode", "toggle_dark_mode"):
        val = {"dark_mode": "true", "light_mode": "false", "toggle_dark_mode": "not dark mode"}[a]
        osa(f'tell application "System Events" to tell appearance preferences to set dark mode to {val}')
        return "Dark mode is now " + ("on." if osa('tell application "System Events" to tell appearance preferences to return dark mode') == "true" else "off.")
    elif a == "lock_screen":
        osa('tell application "System Events" to keystroke "q" using {control down, command down}')
        return "Screen locked."
    elif a == "sleep_display":
        subprocess.run(["pmset", "displaysleepnow"])
        return "Display sleeping."
    elif a == "battery":
        return subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True).stdout.strip()
    else:
        return "action must be volume (value 0-100), volume_up, volume_down, mute, unmute, dark_mode, light_mode, toggle_dark_mode, lock_screen, sleep_display or battery."
    level, muted = vol().split("|")
    return f"Volume is {level}%" + (" (muted)." if muted == "true" else ".")


# --------------------------------------------------------------------------- notes / reminders / calendar

def notes(action: str, title: str = "", body: str = "", query: str = "") -> str:
    a = (action or "").lower()
    if a == "create":
        if not title:
            return "A note needs a title."
        osa('''on run argv
tell application "Notes"
  make new note at folder "Notes" of default account with properties {name:(item 1 of argv), body:("<h1>" & (item 1 of argv) & "</h1><div>" & (item 2 of argv) & "</div>")}
end tell
end run''', title, body.replace("\n", "<br>"))
        n = osa('on run argv\ntell application "Notes" to return count of (notes whose name is (item 1 of argv))\nend run', title)
        return f"Created note “{title}”." if n != "0" else f"Tried to create “{title}” but can't find it afterwards."
    if a in ("search", "find", "list"):
        out = osa('''on run argv
set q to item 1 of argv
set res to ""
tell application "Notes"
  if q is "" then
    set ns to notes 1 thru (my min(10, count of notes))
  else
    set ns to (notes whose name contains q)
  end if
  repeat with n in ns
    set res to res & (name of n) & " (" & (modification date of n as string) & ")" & linefeed
  end repeat
end tell
return res
end run
on min(a, b)
  if a < b then return a
  return b
end min''', query or title)
        return ("Notes:\n" + wrap("notes", out)) if out else f"No notes matching “{query or title}”."
    if a == "read":
        out = osa('''on run argv
tell application "Notes"
  set ns to (notes whose name contains (item 1 of argv))
  if ns is {} then return ""
  return plaintext of item 1 of ns
end tell
end run''', title or query)
        return wrap("note", out) if out else f"No note matching “{title or query}”."
    if a == "append":
        out = osa('''on run argv
tell application "Notes"
  set ns to (notes whose name contains (item 1 of argv))
  if ns is {} then return "none"
  set n to item 1 of ns
  set body of n to (body of n) & "<div>" & (item 2 of argv) & "</div>"
  return name of n
end tell
end run''', title or query, body.replace("\n", "<br>"))
        return f"No note matching “{title or query}”." if out == "none" else f"Added to “{out}”."
    return "action must be create, search, read or append."


def reminder_add(title: str, due: str = "", list_name: str = "") -> str:
    if not title:
        return "What's the reminder?"
    props = "{name:(item 1 of argv)}"
    date_lines = ""
    if due:
        try:
            dt = parse_when(due)
        except ValueError:
            return f"Couldn't understand the time {due!r}; use ISO like 2026-11-14T09:00."
        date_lines = as_date_script("d", dt)
        props = "{name:(item 1 of argv), due date:d, remind me date:d}"
    target = "list (item 2 of argv)" if list_name else "default list"
    osa(f'''on run argv
{date_lines}tell application "Reminders"
  tell {target} to make new reminder with properties {props}
end tell
end run''', title, list_name)
    when = f" for {parse_when(due).strftime('%a %b %-d at %-I:%M %p')}" if due else ""
    n = osa('on run argv\ntell application "Reminders" to return count of (reminders whose name is (item 1 of argv) and completed is false)\nend run', title)
    return f"Reminder “{title}”{when} added." if n != "0" else "Tried to add the reminder but can't find it afterwards."


def calendar_add(title: str, start: str, end: str = "", calendar: str = "", location: str = "") -> str:
    if not title or not start:
        return "An event needs a title and a start time."
    try:
        s = parse_when(start)
        e = parse_when(end) if end else s + timedelta(hours=1)
    except ValueError:
        return "Use ISO times like 2026-11-14T15:00."
    cal = "calendar (item 2 of argv)" if calendar else "first calendar whose writable is true"
    osa(f'''on run argv
{as_date_script("s", s)}{as_date_script("e", e)}tell application "Calendar"
  tell {cal}
    make new event with properties {{summary:(item 1 of argv), start date:s, end date:e, location:(item 3 of argv)}}
  end tell
end tell
end run''', title, calendar, location, timeout=40)
    return f"Added “{title}” on {s.strftime('%a %b %-d, %-I:%M %p')}–{e.strftime('%-I:%M %p')}."


# --------------------------------------------------------------------------- messages / mail (confirm first)

def _contact(name: str) -> list[tuple[str, str]]:
    out = osa('''on run argv
set res to ""
tell application "Contacts"
  repeat with p in (people whose name contains (item 1 of argv))
    repeat with ph in phones of p
      set res to res & (name of p) & "|" & (value of ph) & linefeed
    end repeat
    repeat with em in emails of p
      set res to res & (name of p) & "|" & (value of em) & linefeed
    end repeat
  end repeat
end tell
return res
end run''', name, timeout=30)
    return [tuple(l.split("|", 1)) for l in out.splitlines() if "|" in l]


def _resolve(to: str) -> tuple[str, str] | str:
    to = to.strip()
    if re.fullmatch(r"[+\d\s().-]{7,}", to) or "@" in to:
        return (to, to)
    found = _contact(to)
    if not found:
        return f"No contact named {to}."
    people = {n for n, _ in found}
    if len(people) > 1:
        return f"Several contacts match {to}: {', '.join(sorted(people))}. Ask which one."
    return found[0]


def message_send(to: str, text: str, confirmed: bool = False) -> str:
    if not to or not text:
        return "Need who to send to and what to say."
    r = _resolve(to)
    if isinstance(r, str):
        return r
    name, handle = r
    if not confirmed:
        return (f"PREVIEW — not sent. To {name} ({handle}): “{text}”. Read this back to him and send only "
                f"if he says yes, by calling again with confirmed=true and the same to/text.")
    if refused := _refusal("message_send"):
        return refused
    osa('''on run argv
tell application "Messages"
  set svc to 1st account whose service type = iMessage
  send (item 2 of argv) to participant (item 1 of argv) of svc
end tell
end run''', handle, text)
    return f"Sent to {name} ({handle})."


def email(to: str, subject: str, body: str, confirmed: bool = False) -> str:
    if not to:
        return "Who is the email to?"
    r = _resolve(to)
    if isinstance(r, str):
        return r
    name, addr = r
    if "@" not in addr:
        return f"{name} has no email address in Contacts."
    if not confirmed:
        return (f"PREVIEW — not sent. To {name} <{addr}>, subject “{subject}”: “{body}”. Read it back and send "
                f"only if he says yes (call again with confirmed=true).")
    if refused := _refusal("email"):
        return refused
    osa('''on run argv
tell application "Mail"
  set m to make new outgoing message with properties {subject:(item 2 of argv), content:(item 3 of argv), visible:false}
  tell m to make new to recipient with properties {address:(item 1 of argv)}
  send m
end tell
end run''', addr, subject, body)
    return f"Email sent to {name} <{addr}>."


# --------------------------------------------------------------------------- any app: look, then act

_INSPECT = '''on run argv
set appName to item 1 of argv
set out to ""
set n to 0
tell application "System Events"
  if appName is "" then set appName to name of first application process whose frontmost is true
  tell process appName
    set frontmost to true
    delay 0.3
    if (count of windows) is 0 then return "APP|" & appName & linefeed & "(no windows)"
    set out to "APP|" & appName & linefeed & "WINDOW|" & (name of window 1) & linefeed
    set els to entire contents of window 1
    repeat with e in els
      if n > 160 then exit repeat
      try
        set r to role of e
        if r is in {"AXButton", "AXTextField", "AXTextArea", "AXCheckBox", "AXRadioButton", "AXPopUpButton", "AXMenuButton", "AXLink", "AXTab", "AXSearchField", "AXComboBox", "AXSlider", "AXCell", "AXStaticText", "AXRow", "AXMenuItem"} then
          set nm to ""
          try
            set nm to name of e
          end try
          if nm is missing value then set nm to ""
          set ds to ""
          try
            set ds to description of e
          end try
          if ds is missing value then set ds to ""
          set v to ""
          try
            set v to value of e as string
          end try
          if v is missing value or v is "missing value" then set v to ""
          if (nm & ds & v) is not "" then
            set out to out & r & "|" & nm & "|" & ds & "|" & (text 1 thru (my min(60, length of v)) of v) & linefeed
            set n to n + 1
          end if
        end if
      end try
    end repeat
  end tell
end tell
return out
end run
on min(a, b)
  if a < b then return a
  return b
end min'''


def ui_inspect(app_name: str = "") -> str:
    try:
        raw = osa(_INSPECT, app_name or "", timeout=45)
    except Exception as e:
        return f"Couldn't read the app's window: {e}. (Accessibility permission may be needed.)"
    lines = []
    for l in raw.splitlines():
        parts = l.split("|")
        if parts[0] in ("APP", "WINDOW"):
            lines.append(f"{parts[0].title()}: {parts[1]}")
            continue
        role, nm, ds, v = (parts + ["", "", "", ""])[:4]
        label = nm or ds
        lines.append(f"- {role[2:]}: “{label}”" + (f" = {v}" if v and v != label else ""))
    if not lines:
        return "Nothing readable in that window."
    # Whatever is on screen (a web page, an email) was written by someone else.
    return "Window contents:\n" + wrap("screen", "\n".join(lines))


_ACT = '''on run argv
set appName to item 1 of argv
set target to item 2 of argv
set act to item 3 of argv
set txt to item 4 of argv
tell application "System Events"
  if appName is "" then set appName to name of first application process whose frontmost is true
  tell process appName
    set frontmost to true
    delay 0.2
    set els to entire contents of window 1
    set hit to missing value
    repeat with pass from 1 to 2
      repeat with e in els
        try
          set nm to ""
          try
            set nm to name of e
          end try
          if nm is missing value then set nm to ""
          set ds to ""
          try
            set ds to description of e
          end try
          if ds is missing value then set ds to ""
          if pass is 1 then
            if nm is target or ds is target then set hit to e
          else
            if nm contains target or ds contains target then set hit to e
          end if
          if hit is not missing value then exit repeat
        end try
      end repeat
      if hit is not missing value then exit repeat
    end repeat
    if hit is missing value then return "NOTFOUND"
    if act is "click" then
      try
        perform action "AXPress" of hit
      on error
        click hit
      end try
    else if act is "set_value" then
      set focused of hit to true
      set value of hit to txt
    end if
    return (role of hit) & "|" & target
  end tell
end tell
end run'''

_KEYCODES = {"return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51,
             "escape": 53, "esc": 53, "left": 123, "right": 124, "down": 125, "up": 126,
             "pageup": 116, "pagedown": 121, "home": 115, "end": 119}
_MODS = {"cmd": "command down", "command": "command down", "shift": "shift down",
         "option": "option down", "alt": "option down", "ctrl": "control down", "control": "control down"}


def ui_act(action: str, app_name: str = "", target: str = "", text: str = "", keys: str = "") -> str:
    a = (action or "").lower()
    if refused := _refusal("ui_act"):
        return refused
    if app_name:
        subprocess.run(["open", "-a", app_name], capture_output=True)
        time.sleep(0.6)
    if a in ("click", "set_value"):
        if not target:
            return "Which element? Use its label from ui_inspect."
        try:
            res = osa(_ACT, app_name, target, a, text, timeout=45)
        except Exception as e:
            return f"Failed: {e}"
        if res == "NOTFOUND":
            return f"No element labelled “{target}” in the front window. Run ui_inspect to see the real labels."
        time.sleep(0.8)
        return f"Did {a} on {res.split('|')[0][2:]} “{target}”. Window now:\n{ui_inspect(app_name)[:1500]}"
    if a == "type":
        if not text:
            return "Type what?"
        osa('on run argv\ntell application "System Events" to keystroke (item 1 of argv)\nend run', text)
        return f"Typed “{text[:80]}” into {frontmost_app()}."
    if a in ("keys", "press_keys", "shortcut"):
        if not keys:
            return "Which keys? e.g. cmd+n, return, cmd+shift+t"
        parts = [k.strip().lower() for k in keys.replace(" ", "").split("+") if k.strip()]
        mods = [_MODS[k] for k in parts if k in _MODS]
        main = [k for k in parts if k not in _MODS]
        if len(main) != 1:
            return "Give exactly one main key, like cmd+n."
        k = main[0]
        using = f" using {{{', '.join(mods)}}}" if mods else ""
        if k in _KEYCODES:
            osa(f'tell application "System Events" to key code {_KEYCODES[k]}{using}')
        elif len(k) == 1:
            osa(f'on run argv\ntell application "System Events" to keystroke (item 1 of argv){using}\nend run', k)
        else:
            return f"Unknown key {k}."
        time.sleep(0.6)
        return f"Pressed {keys} in {frontmost_app()}."
    return "action must be click, set_value, type or keys."


_BLOCKED = re.compile(r"do\s+shell\s+script|\bdelete\b|empty\s+(the\s+)?trash|\berase\b|\bformat\b|password|keychain|"
                      r"\bsudo\b|administrator privileges|\brm\s|move\s.+\sto\s+trash|\bpurchase\b|\bbuy\b|"
                      # other ways to run code: Terminal, nested scripts, raw Apple events, ObjC bridge
                      r"do\s+script|run\s+script|do\s+javascript|«|use\s+framework|NSTask|NSAppleScript", re.I)


def run_applescript(script: str) -> str:
    if not script.strip():
        return "Empty script."
    if _BLOCKED.search(script):
        return ("Refused: that script deletes, erases, buys, touches passwords or runs shell commands. "
                "Tell him what you would need to do and let him do it himself.")
    if refused := _refusal("run_applescript"):
        return refused
    try:
        out = osa(script, timeout=60)
    except Exception as e:
        return f"Script error: {e}"
    return ("OK. Result:\n" + wrap("script result", out, cap=1500)) if out else "OK (no result returned)."


# --------------------------------------------------------------------------- timers

_TIMERS = os.path.join(os.environ.get("JARVIS_DATA_DIR") or
                       os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "jarvis"),
                       "timers.json")

# Runs detached so the timer outlives this MCP child (and any brain restart).
# The label arrives as argv, never spliced into the shell or AppleScript source.
_TIMER_SH = ('sleep "$1"; osascript -e "on run argv" '
             '-e "display notification (item 1 of argv) with title \\"Timer done\\" sound name \\"Glass\\"" '
             '-e "end run" "$2"; for i in 1 2 3; do afplay /System/Library/Sounds/Glass.aiff; done')


def _load_timers() -> list[dict]:
    try:
        with open(_TIMERS) as f:
            items = json.load(f)
    except (OSError, ValueError):
        return []
    now = time.time()
    return [t for t in items if t.get("ends", 0) > now]


def _save_timers(items: list[dict]) -> None:
    os.makedirs(os.path.dirname(_TIMERS), exist_ok=True)
    with open(_TIMERS, "w") as f:
        json.dump(items, f)


def _left(t: dict) -> str:
    s = max(0, int(t["ends"] - time.time()))
    m, s = divmod(s, 60)
    return f"{m} min {s} s" if m else f"{s} s"


def timer(action: str = "set", minutes: float = 0, seconds: float = 0, label: str = "") -> str:
    items = _load_timers()
    if action == "set":
        total = int(round(float(minutes or 0) * 60 + float(seconds or 0)))
        if total <= 0:
            return "How long should the timer be?"
        if total > 24 * 3600:
            return "Timers max out at 24 hours; use reminder_add for anything longer."
        name = label.strip() or (f"{total // 60} minute timer" if total % 60 == 0 else f"{total} second timer")
        proc = subprocess.Popen(["/bin/sh", "-c", _TIMER_SH, "sh", str(total), name],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        items.append({"label": name, "ends": time.time() + total, "pid": proc.pid})
        _save_timers(items)
        return f"Timer “{name}” set; it goes off at {datetime.fromtimestamp(items[-1]['ends']).strftime('%-I:%M:%S %p')} with a chime and a notification."
    if action == "list":
        if not items:
            return "No timers running."
        return "; ".join(f"“{t['label']}”: {_left(t)} left" for t in items)
    if action == "cancel":
        if not items:
            return "No timers running."
        hit = [t for t in items if label and label.lower() in t["label"].lower()] or (items if not label else [])
        if not hit:
            return f"No timer matching “{label}”. Running: " + ", ".join(t["label"] for t in items)
        for t in hit:
            try:
                os.killpg(t["pid"], 15)
            except OSError:
                pass
        _save_timers([t for t in items if t not in hit])
        return "Cancelled " + ", ".join(f"“{t['label']}”" for t in hit) + "."
    return "action must be set, list or cancel."


# --------------------------------------------------------------------------- MCP plumbing

S = lambda **p: {"type": "object", "properties": p}
STR = lambda d: {"type": "string", "description": d}
TOOLS = {
    "app": (app, "Open, quit, hide or list apps on his Mac. Reports what is in front afterwards.",
            S(action=STR("open | quit | hide | list"), name=STR("app name, e.g. Spotify"))),
    "media": (media, "Control music. action=play with query plays a song/album/playlist/artist (Spotify by default, or player=music); play/pause/toggle/next/previous without query control playback; now_playing reports. Always reports what is ACTUALLY playing afterwards — check it matches what he asked for.",
              S(action=STR("play | pause | toggle | next | previous | now_playing"), query=STR("what to play"),
                kind=STR("track | album | playlist | artist (default track)"), player=STR("spotify (default) | music"))),
    "system": (system, "Mac settings: volume (value 0-100), volume_up, volume_down, mute, unmute, dark_mode, light_mode, toggle_dark_mode, lock_screen, sleep_display, battery.",
               S(action=STR("see description"), value={"type": "integer"})),
    "notes": (notes, "Apple Notes: create (title, body), search (query), read (title), append (title, body).",
              S(action=STR("create | search | read | append"), title=STR("note title"), body=STR("text"), query=STR("search text"))),
    "timer": (timer, "Countdown timers. ALWAYS use this for 'set a timer' / 'X minutes' requests — never the Clock app or UI automation. set (minutes and/or seconds, optional label) returns instantly; list reports time left; cancel (label, or all if none).",
              S(action=STR("set | list | cancel"), minutes={"type": "number"}, seconds={"type": "number"}, label=STR("optional name, e.g. pasta"))),
    "reminder_add": (reminder_add, "Add an Apple Reminder, optionally due at an ISO local time (work it out from the current time you were given).",
                     S(title=STR("what to remember"), due=STR("ISO local time, e.g. 2026-11-14T09:00 (optional)"), list_name=STR("Reminders list (optional)"))),
    "calendar_add": (calendar_add, "Create an Apple Calendar event. Times are ISO local; end defaults to one hour after start.",
                     S(title=STR("event title"), start=STR("ISO local start"), end=STR("ISO local end (optional)"),
                       calendar=STR("calendar name (optional)"), location=STR("optional"))),
    "message_send": (message_send, "Send an iMessage/text. TWO STEPS: call without confirmed to get a preview, read it back to him, and only after he says yes call again with confirmed=true.",
                     S(to=STR("contact name, phone or email"), text=STR("message"), confirmed={"type": "boolean"})),
    "email": (email, "Send an email from Apple Mail. TWO STEPS exactly like message_send: preview, read back, send only after his yes with confirmed=true.",
              S(to=STR("contact name or address"), subject=STR("subject"), body=STR("body"), confirmed={"type": "boolean"})),
    "ui_inspect": (ui_inspect, "Read the buttons, fields, tabs and text in an app's front window (default: the front app). Use BEFORE ui_act in any app without a dedicated tool, so you click real labels instead of guessing.",
                   S(app_name=STR("app name (optional)"))),
    "ui_act": (ui_act, "Operate any app: click an element by its label from ui_inspect, set_value of a field, type text, or press keys (e.g. cmd+n, return, cmd+shift+t). Click/set_value report the window afterwards so you can confirm it worked.",
               S(action=STR("click | set_value | type | keys"), app_name=STR("app (optional, brings it forward)"),
                 target=STR("element label, exact from ui_inspect"), text=STR("text to type or set"), keys=STR("key combo"))),
    "run_applescript": (run_applescript, "Last resort for something no other tool covers: run an AppleScript you write. Deleting, erasing, buying, passwords and shell commands are refused.",
                        S(script=STR("AppleScript source"))),
}


def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid, method = req.get("id"), req.get("method")
        if mid is None:
            continue
        if method == "initialize":
            result = {"protocolVersion": req.get("params", {}).get("protocolVersion", "2024-11-05"),
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "mac", "version": "1.0"}}
        elif method == "tools/list":
            result = {"tools": [{"name": n, "description": d, "inputSchema": sch} for n, (_, d, sch) in TOOLS.items()]}
        elif method == "tools/call":
            name = req["params"].get("name")
            args = req["params"].get("arguments") or {}
            if name not in TOOLS:
                _send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown tool {name}"}})
                continue
            try:
                text = TOOLS[name][0](**args)
            except Exception as e:
                text = f"{name} failed: {e}"
            result = {"content": [{"type": "text", "text": text}]}
        elif method == "ping":
            result = {}
        else:
            _send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}})
            continue
        _send({"jsonrpc": "2.0", "id": mid, "result": result})


if __name__ == "__main__":
    main()
