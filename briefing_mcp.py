#!/usr/bin/env python3
"""
briefing_mcp.py — a tiny local MCP server (stdio, JSON-RPC) that gives JARVIS
your day: Apple Calendar, Apple Reminders, the weather and a few headlines.

No accounts and no API keys. Calendar and Reminders are read through
AppleScript (macOS asks once for permission); weather comes from wttr.in
(located by your IP unless BRIEFING_LOCATION is set, e.g. "Austin, TX");
headlines from a public RSS feed (BRIEFING_NEWS_FEED to change it).

Declared in data/jarvis/connections.json as the "briefing" server.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from untrusted_text import wrap

LOCATION = os.getenv("BRIEFING_LOCATION", "")
NEWS_FEED = os.getenv("BRIEFING_NEWS_FEED", "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en")
UA = {"User-Agent": "jarvis-briefing/1.0"}


# --------------------------------------------------------------------------- data

def _osascript(script: str, timeout: float = 25.0) -> str:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "osascript failed")
    return r.stdout.strip()


def calendar_events(days_ahead: int = 0) -> str:
    days_ahead = max(0, min(int(days_ahead or 0), 14))
    script = f'''
set startD to current date
set hours of startD to 0
set minutes of startD to 0
set seconds of startD to 0
set endD to startD + ({days_ahead} + 1) * days
set out to ""
tell application "Calendar"
  repeat with c in calendars
    try
      set evs to (every event of c whose start date < endD and end date > startD)
      repeat with e in evs
        set sd to start date of e
        set ad to allday event of e
        set out to out & (sd as «class isot» as string) & "|" & ad & "|" & (summary of e) & "|" & (name of c) & linefeed
      end repeat
    end try
  end repeat
end tell
return out'''
    try:
        raw = _osascript(script, timeout=40)
    except Exception as e:
        return f"Calendar unavailable: {e}"
    rows = []
    for line in raw.splitlines():
        parts = line.split("|", 3)
        if len(parts) < 4:
            continue
        iso, allday, title, cal = parts
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            continue
        rows.append((dt, allday == "true", title.strip(), cal.strip()))
    if not rows:
        return "No calendar events " + ("today." if days_ahead == 0 else f"in the next {days_ahead + 1} days.")
    rows.sort(key=lambda r: r[0])
    out = []
    for dt, allday, title, cal in rows:
        when = dt.strftime("%a %b %d") + (" (all day)" if allday else dt.strftime(" %-I:%M %p"))
        out.append(f"- {when}: {title} [{cal}]")
    # Event titles come from whoever sent the invitation.
    return "Calendar:\n" + wrap("calendar", "\n".join(out))


def reminders() -> str:
    script = '''
set out to ""
set cutoff to (current date) + 1 * days
tell application "Reminders"
  repeat with l in lists
    try
      set rs to (every reminder of l whose completed is false)
      repeat with r in rs
        set d to ""
        try
          set dd to due date of r
          if dd is not missing value then set d to (dd as «class isot» as string)
        end try
        set out to out & d & "|" & (name of r) & "|" & (name of l) & linefeed
      end repeat
    end try
  end repeat
end tell
return out'''
    try:
        raw = _osascript(script, timeout=40)
    except Exception as e:
        return f"Reminders unavailable: {e}"
    now = datetime.now()
    due, undated = [], []
    for line in raw.splitlines():
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        iso, name, lst = parts
        if iso:
            try:
                dt = datetime.fromisoformat(iso)
            except ValueError:
                continue
            if (dt - now).days < 1:   # overdue or due within ~a day
                tag = "OVERDUE" if dt < now else dt.strftime("%-I:%M %p")
                due.append((dt, f"- {name.strip()} ({tag}) [{lst.strip()}]"))
        else:
            undated.append(f"- {name.strip()} [{lst.strip()}]")
    lines = [s for _, s in sorted(due)]
    if not lines and not undated:
        return "No open reminders."
    out = "Reminders due today or overdue:\n" + ("\n".join(lines) if lines else "- none")
    if undated:
        out += f"\nOpen reminders with no date ({len(undated)}):\n" + "\n".join(undated[:8])
    # Shared lists hold other people's words.
    return wrap("reminders", out)


def open_app(name: str) -> str:
    """Open (or bring forward) a Mac app by name, e.g. 'Safari', 'Spotify', 'Notes'."""
    name = (name or "").strip()
    if not name or "/" in name:
        return "Give me an app name, like Spotify or Notes."
    r = subprocess.run(["open", "-a", name], capture_output=True, text=True, timeout=15)
    if r.returncode == 0:
        return f"Opened {name}."
    found = subprocess.run(["mdfind", f"kMDItemKind == 'Application' && kMDItemDisplayName == '*{name}*'cd"],
                           capture_output=True, text=True, timeout=10).stdout.split("\n")
    apps = sorted({os.path.basename(p)[:-4] for p in found if p.endswith(".app")})[:5]
    return f"Couldn't find an app called {name}." + (f" Close matches: {', '.join(apps)}." if apps else "")


def quit_app(name: str) -> str:
    """Quit a Mac app by name."""
    name = (name or "").strip().replace('"', "")
    if not name:
        return "Which app?"
    try:
        _osascript(f'tell application "{name}" to quit', timeout=15)
        return f"Quit {name}."
    except Exception as e:
        return f"Couldn't quit {name}: {e}"


NWS_UA = {"User-Agent": "jarvis-briefing (personal assistant)", "Accept": "application/geo+json"}
# "lat,lon" — set BRIEFING_LATLON in .env to move it. Default: The Woodlands, TX.
_LOCATION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "jarvis", "location.json")
_nws_cache: dict = {}


def latlon() -> str:
    """Where he is: the location his JARVIS tab last shared (Chrome, Wi-Fi based),
    else BRIEFING_LATLON from .env, else The Woodlands, TX."""
    try:
        with open(_LOCATION_FILE) as f:
            d = json.load(f)
        return f"{float(d['lat']):.4f},{float(d['lon']):.4f}"
    except Exception:
        return os.getenv("BRIEFING_LATLON", "30.1658,-95.4613")


def _nws(url: str) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers=NWS_UA), timeout=12) as r:
        return json.load(r)


def _nws_point() -> dict:
    ll = latlon()
    if _nws_cache.get("ll") != ll:
        _nws_cache.clear()
        _nws_cache["ll"] = ll
    if "point" not in _nws_cache:
        p = _nws(f"https://api.weather.gov/points/{ll}")["properties"]
        stations = _nws(p["observationStations"])["features"]
        _nws_cache["point"] = {
            "city": p["relativeLocation"]["properties"]["city"],
            "state": p["relativeLocation"]["properties"]["state"],
            "forecast": p["forecast"],
            "station": stations[0]["properties"]["stationIdentifier"] if stations else "",
        }
    return _nws_cache["point"]


def _f(c):
    return None if c is None else round(c * 9 / 5 + 32)


def _nws_now() -> tuple[dict, list, list]:
    pt = _nws_point()
    periods = _nws(pt["forecast"])["properties"]["periods"]
    obs = {}
    if pt["station"]:
        try:
            obs = _nws(f"https://api.weather.gov/stations/{pt['station']}/observations/latest")["properties"]
        except Exception:
            obs = {}
    try:
        alerts = _nws(f"https://api.weather.gov/alerts/active?point={latlon()}")["features"]
    except Exception:
        alerts = []
    return {"pt": pt, "obs": obs}, periods, alerts


def weather() -> str:
    """National Weather Service: current conditions, the next forecast periods, active alerts."""
    try:
        ctx, periods, alerts = _nws_now()
    except Exception as e:
        return f"Weather unavailable (National Weather Service): {e}"
    pt, obs = ctx["pt"], ctx["obs"]
    lines = [f"Weather for {pt['city']}, {pt['state']} (National Weather Service):"]
    t = _f((obs.get("temperature") or {}).get("value"))
    if t is not None:
        lines.append(f"Now: {t}°F, {obs.get('textDescription') or 'conditions unknown'}.")
    for per in periods[:3]:
        pop = (per.get("probabilityOfPrecipitation") or {}).get("value")
        lines.append(f"{per['name']}: {per['temperature']}°{per['temperatureUnit']}, {per['shortForecast']}"
                     + (f", {pop}% chance of rain" if pop else "") + ".")
    for a in alerts[:3]:
        lines.append(f"ALERT: {a['properties'].get('headline') or a['properties'].get('event')}")
    return "\n".join(lines)


def weather_brief() -> str:
    """One short spoken sentence, for the wake-up greeting. Empty on failure."""
    try:
        ctx, periods, alerts = _nws_now()
    except Exception:
        return ""
    obs = ctx["obs"]
    t = _f((obs.get("temperature") or {}).get("value"))
    desc = (obs.get("textDescription") or "").lower()
    out = f"It's {t} degrees and {desc} outside." if t is not None and desc else (f"It's {t} degrees outside." if t is not None else "")
    if periods:
        nxt = periods[0]
        pop = (nxt.get("probabilityOfPrecipitation") or {}).get("value")
        out += f" {nxt['name']}: {nxt['shortForecast'].lower()}, {'high' if nxt.get('isDaytime') else 'low'} of {nxt['temperature']}"
        out += f", {pop} percent chance of rain." if pop and pop >= 20 else "."
    if alerts:
        out += f" Heads up: {alerts[0]['properties'].get('event')} in effect."
    return out.strip()


def news_headlines(count: int = 5) -> str:
    count = max(1, min(int(count or 5), 10))
    try:
        with urllib.request.urlopen(urllib.request.Request(NEWS_FEED, headers=UA), timeout=12) as r:
            root = ET.fromstring(r.read())
        titles = [i.findtext("title", "").strip() for i in root.iter("item")]
        titles = [t for t in titles if t][:count]
        return ("Top headlines:\n" + wrap("headlines", "\n".join(f"- {t}" for t in titles))) if titles else "No headlines found."
    except Exception as e:
        return f"Headlines unavailable: {e}"


def morning_briefing() -> str:
    with ThreadPoolExecutor(4) as ex:
        parts = [ex.submit(f) for f in (weather, calendar_events, reminders, news_headlines)]
        body = "\n\n".join(p.result() for p in parts)
    return datetime.now().strftime("Today is %A, %B %-d, %-I:%M %p.") + "\n\n" + body


# --------------------------------------------------------------------------- MCP

TOOLS = {
    "morning_briefing": (morning_briefing, "Everything for a morning briefing in one call: weather, today's Apple Calendar events, due/overdue Apple Reminders and top news headlines.", {}),
    "calendar_events": (calendar_events, "Apple Calendar events from today through days_ahead days from now (0 = today only).",
                        {"days_ahead": {"type": "integer", "description": "0 = today, 1 = today and tomorrow, up to 14"}}),
    "reminders": (reminders, "Open Apple Reminders: due today or overdue, plus undated ones.", {}),
    "weather": (weather, "Current conditions, forecast and any active weather alerts from the National Weather Service.", {}),
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
        if mid is None:          # notification
            continue
        if method == "initialize":
            result = {"protocolVersion": req.get("params", {}).get("protocolVersion", "2024-11-05"),
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "briefing", "version": "1.0"}}
        elif method == "tools/list":
            result = {"tools": [{"name": n, "description": d,
                                 "inputSchema": {"type": "object", "properties": p}}
                                for n, (_, d, p) in TOOLS.items()]}
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
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        print(morning_briefing())
    else:
        main()
