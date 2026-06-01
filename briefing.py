"""
JARVIS Morning Briefing — gather the facts for the post-startup briefing.

Pulls together the data sources that aren't already in server.py:
  * traffic   — Google Directions API (live, traffic-aware ETA)
  * weather   — Open-Meteo daily forecast (no key) for clothing advice
  * portfolio — runs the user's track.py to refresh prices, parses the totals

Mail, calendar and crypto-sentiment reuse the existing server.py helpers.
Each function returns plain facts; server.py composes them into a spoken,
language-appropriate briefing via the LLM.
"""

import asyncio
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("jarvis.briefing")

# Home → office, fixed for the user.
HOME_ADDRESS = os.getenv("BRIEFING_HOME", "1146G Route des Mermes, 74140 Veigy-Foncenex, France")
OFFICE_ADDRESS = os.getenv("BRIEFING_OFFICE", "Barclays Bank, 28-20 Chemin Grange-Canal, 1204 Geneva, Switzerland")

# Veigy-Foncenex coordinates for the weather forecast.
WEATHER_LAT = float(os.getenv("BRIEFING_LAT", "46.2755"))
WEATHER_LON = float(os.getenv("BRIEFING_LON", "6.2925"))

PORTFOLIO_DIR = Path(os.getenv(
    "BRIEFING_PORTFOLIO_DIR",
    str(Path.home() / "Desktop" / "research-balanced-investment-opportunities"),
))


def _get(url: str, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "JARVIS/1.0"})
    return urllib.request.urlopen(req, timeout=timeout).read()


# ---- Traffic -------------------------------------------------------------

async def get_traffic() -> dict:
    """Live traffic-aware ETA home → office via Google Directions."""
    key = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
    if not key:
        return {"ok": False, "reason": "no_key"}

    def _call():
        params = {
            "origin": HOME_ADDRESS, "destination": OFFICE_ADDRESS,
            "departure_time": "now", "traffic_model": "best_guess",
            "mode": "driving", "key": key,
        }
        url = "https://maps.googleapis.com/maps/api/directions/json?" + urllib.parse.urlencode(params)
        return json.loads(_get(url))

    try:
        data = await asyncio.to_thread(_call)
    except Exception as e:
        log.warning(f"traffic fetch failed: {e}")
        return {"ok": False, "reason": str(e)}

    if data.get("status") != "OK":
        return {"ok": False, "reason": data.get("error_message") or data.get("status")}

    leg = data["routes"][0]["legs"][0]
    normal = leg["duration"]["value"] // 60
    traffic = leg.get("duration_in_traffic", {}).get("value", leg["duration"]["value"]) // 60
    delay = traffic - normal
    if delay >= 8:
        condition = "heavy traffic"
    elif delay >= 3:
        condition = "moderate traffic"
    else:
        condition = "clear roads"
    return {
        "ok": True,
        "distance": leg["distance"]["text"],
        "eta_min": traffic,
        "normal_min": normal,
        "delay_min": delay,
        "condition": condition,
        "route": data["routes"][0].get("summary", ""),
        "warnings": data["routes"][0].get("warnings", []),
    }


# ---- Weather -------------------------------------------------------------

_WCODE = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
    81: "rain showers", 82: "violent rain showers", 95: "thunderstorm",
    96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


async def get_weather() -> dict:
    """Today's forecast (high/low, conditions, rain chance) for the home area."""
    def _call():
        params = {
            "latitude": WEATHER_LAT, "longitude": WEATHER_LON,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weathercode",
            "current": "temperature_2m,weathercode",
            "timezone": "auto", "forecast_days": 1,
        }
        url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
        return json.loads(_get(url))

    try:
        d = await asyncio.to_thread(_call)
        daily = d["daily"]
        code = daily["weathercode"][0]
        return {
            "ok": True,
            "high_c": round(daily["temperature_2m_max"][0]),
            "low_c": round(daily["temperature_2m_min"][0]),
            "current_c": round(d.get("current", {}).get("temperature_2m", daily["temperature_2m_max"][0])),
            "rain_chance": daily["precipitation_probability_max"][0],
            "conditions": _WCODE.get(code, "mixed conditions"),
        }
    except Exception as e:
        log.warning(f"weather fetch failed: {e}")
        return {"ok": False, "reason": str(e)}


# ---- Portfolio -----------------------------------------------------------

async def get_portfolio() -> dict:
    """Refresh prices via the user's track.py, parse totals + movers."""
    script = PORTFOLIO_DIR / "track.py"
    if not script.exists():
        return {"ok": False, "reason": "no_script"}
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", str(script),
            cwd=str(PORTFOLIO_DIR),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except Exception as e:
        log.warning(f"portfolio refresh failed: {e}")
        return {"ok": False, "reason": str(e)}

    text = out.decode(errors="replace")
    positions = []
    total_value = total_gain_pct = None
    for line in text.splitlines():
        # e.g. "SPCE  162.25  $7.83  $1,269.90  $267.16  +26.6%"
        m = re.match(r"\s*([A-Z]{2,6})\s+[\d.]+\s+\$[\d,]+\.\d+\s+\$[\d,\-]+\.\d+\s+\$[\d,\-]+\.\d+\s+([+\-][\d.]+)%", line)
        if m:
            positions.append({"ticker": m.group(1), "gain_pct": float(m.group(2))})
        t = re.search(r"TOTAL\s+\$([\d,]+\.\d+)\s+\$[\d,\-]+\.\d+\s+([+\-][\d.]+)%", line)
        if t:
            total_value = t.group(1)
            total_gain_pct = float(t.group(2))

    movers = sorted(positions, key=lambda p: p["gain_pct"], reverse=True)
    return {
        "ok": total_value is not None,
        "total_value": total_value,
        "total_gain_pct": total_gain_pct,
        "best": movers[0] if movers else None,
        "worst": movers[-1] if movers else None,
        "dashboard": str(PORTFOLIO_DIR / "dashboard.html"),
    }


async def open_dashboard_window() -> None:
    """Open the portfolio dashboard in a small Chrome app window."""
    dash = PORTFOLIO_DIR / "dashboard.html"
    if not dash.exists():
        return
    url = f"file://{dash}"
    script = f'''
tell application "Google Chrome"
    make new window
    set URL of active tab of front window to "{url}"
    set bounds of front window to {{60, 80, 560, 720}}
end tell
'''
    try:
        await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        log.warning(f"open dashboard window failed: {e}")
