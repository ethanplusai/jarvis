"""
Background context refresh — keeps _ctx_cache up to date with screen
layout and weather. Runs in a daemon thread so it never blocks the
asyncio event loop.
"""

import contextlib
import json
import logging
import subprocess
import threading
import time
import urllib.request

from screen import format_windows_for_context

log = logging.getLogger("jarvis.context_cache")


_SCREEN_SCRIPT = """
set windowList to ""
tell application "System Events"
    set frontApp to name of first application process whose frontmost is true
    set visibleApps to every application process whose visible is true
    repeat with proc in visibleApps
        set appName to name of proc
        try
            set winCount to count of windows of proc
            if winCount > 0 then
                repeat with w in (windows of proc)
                    try
                        set winTitle to name of w
                        if winTitle is not "" and winTitle is not missing value then
                            set windowList to windowList & appName & "|||" & winTitle & "|||" & (appName = frontApp) & linefeed
                        end if
                    end try
                end repeat
            end if
        end try
    end repeat
end tell
return windowList
"""

_WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude=27.77&longitude=-82.64"
    "&current=temperature_2m,weathercode&temperature_unit=fahrenheit"
)


def start_context_refresh(ctx_cache: dict, interval_seconds: int = 30) -> None:
    """Start a daemon thread that refreshes screen + weather in ctx_cache."""

    def _worker() -> None:
        while True:
            with contextlib.suppress(Exception):
                proc = subprocess.run(
                    ["osascript", "-e", _SCREEN_SCRIPT],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    windows = []
                    for line in proc.stdout.strip().split("\n"):
                        parts = line.strip().split("|||")
                        if len(parts) >= 3:
                            windows.append(
                                {
                                    "app": parts[0].strip(),
                                    "title": parts[1].strip(),
                                    "frontmost": parts[2].strip().lower() == "true",
                                }
                            )
                    if windows:
                        ctx_cache["screen"] = format_windows_for_context(windows)

            with (
                contextlib.suppress(Exception),
                urllib.request.urlopen(_WEATHER_URL, timeout=3) as resp,  # noqa: S310 — hardcoded open-meteo URL
            ):
                d = json.loads(resp.read()).get("current", {})
                temp = d.get("temperature_2m", "?")
                ctx_cache["weather"] = f"Current weather in St. Petersburg, FL: {temp}°F"

            time.sleep(interval_seconds)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    log.info("Context refresh thread started")
