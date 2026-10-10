"""What the CLI last told us about the subscription's limits.

JARVIS runs on a Claude Code subscription, not on an API key — `claude_env`
scrubs every `ANTHROPIC_*` variable precisely so the CLI can never bill one.
So there is no spend to report. What there IS, and what the user actually
asks for out loud ("what's my session limit"), is how much of the five-hour
and seven-day windows has been used.

That number arrives in exactly one place: a `rate_limit_event` on the brain's
stdout, which the CLI sends only while a turn is in flight. The brain used it
for a backoff and threw it away. This module is where it is kept.

  record(rate_limit_info)   persist one observation (called by brain.py)
  latest()                  the raw stored record, or None
  snapshot()                what /api/usage returns

THE ONE RULE
------------
Absence is a state, and it is preserved as one. A window nobody has observed
comes back with `utilization: None` — never 0, never a full green gauge. A
user who has burned 84% of their week and sees an empty gauge because the
brain has not taken a turn yet is worse off than one who is told "not
measured". Every reading is stamped with when it was taken, per window, so
the reader can tell four minutes ago from four hours ago.

ENCODING
--------
`utilization` is a FRACTION in the payload: 0.78 means 78%. Measured live on
2026-09-02 — the CLI sent `"utilization": 0.76` on the same event that the
brain logged as "seven_day window at 76%". A fraction cannot exceed 1, so a
value above 1 is read as an already-percent encoding rather than doubled up
into 5500%.
"""

import json
import logging
import os
import tempfile
import time
from typing import Any, Optional

import data_paths

log = logging.getLogger("jarvis.usage_store")

# How old a reading may be before the UI must say so. A five-hour window can
# move a long way in half an hour of work, so half an hour is the line.
STALE_AFTER_SEC = 30 * 60

# The windows the CLI reports today, in the order a human cares about them.
# Both are always present in a snapshot even when never observed — a named
# window saying "not measured" beats a window that quietly isn't there.
KNOWN_WINDOWS = ("five_hour", "seven_day")

WINDOW_LABELS = {
    "five_hour": "5-hour session",
    "seven_day": "7-day week",
    "seven_day_opus": "7-day Opus",
}


def window_label(key: str) -> str:
    return WINDOW_LABELS.get(key, key.replace("_", " "))


# ── coercion: anything that is not plainly a number stays None ──────────────

def _percent(raw: Any) -> Optional[float]:
    """A utilisation fraction as a 0–100 percentage, or None if it isn't one."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        return None
    pct = value * 100.0 if value <= 1.0 else value
    return round(min(pct, 100.0), 1)


def _stored_percent(raw: Any) -> Optional[float]:
    """A percentage that `record` has ALREADY normalised, validated but never
    rescaled.

    `_percent` reads a value <= 1 as a fraction, which is right for the CLI's
    payload and wrong for anything read back out of the store: a window
    truthfully sitting at 1.0% satisfies `value <= 1.0` and was being
    multiplied into a full 100% gauge. The bug hid itself, because any window
    above 1% passes through `_percent` unchanged — a user at 9% and 1% saw one
    correct gauge and one pinned to the top.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        return None
    return round(min(value, 100.0), 1)


def _epoch(raw: Any) -> Optional[float]:
    """An epoch-seconds timestamp, or None. Milliseconds are rescaled so a
    future CLI change reads as a time rather than as the year 58000."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if value != value or value <= 0 or value in (float("inf"), float("-inf")):
        return None
    if value > 1e11:
        value /= 1000.0
    return value


def _text(raw: Any) -> str:
    return raw if isinstance(raw, str) else ""


# ── reading and writing the file ────────────────────────────────────────────

def latest() -> Optional[dict]:
    """The stored observation, or None if there has never been one.

    A missing, unreadable or corrupt file is "no observation" — the dashboard
    must degrade to "not measured", never to an error or to zeroes.
    """
    try:
        body = json.loads(data_paths.usage_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning(f"usage_store: unreadable observation ignored ({e})")
        return None
    if not isinstance(body, dict) or not isinstance(body.get("windows"), dict):
        log.warning("usage_store: observation has the wrong shape; ignoring it")
        return None
    return body


def _write(record: dict) -> None:
    """Replace the file atomically. A half-written file would read as corrupt
    on the very next request, which is the one moment it matters."""
    path = data_paths.usage_path()
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".usage-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(record, fh, indent=2, sort_keys=True)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as e:
        # Losing a usage reading must never take a turn down with it.
        log.warning(f"usage_store: could not persist the observation ({e})")


def record(info: Any, *, now: Optional[float] = None) -> Optional[dict]:
    """Persist one `rate_limit_info` payload. Returns the stored record.

    Windows are merged, newest reading per window wins, and each keeps the
    timestamp of the event it actually came from — an event that names only
    `five_hour` must not re-stamp last hour's `seven_day` reading as current.
    """
    if not isinstance(info, dict):
        return None
    when = time.time() if now is None else float(now)

    stored = latest() or {}
    windows: dict[str, dict] = {
        k: dict(v) for k, v in (stored.get("windows") or {}).items()
        if isinstance(v, dict)
    }

    status = _text(info.get("status"))
    named = _text(info.get("rateLimitType"))

    incoming: dict[str, dict] = {}
    unified = info.get("unifiedWindows")
    if isinstance(unified, dict):
        for key, raw in unified.items():
            if not isinstance(key, str) or not isinstance(raw, dict):
                continue
            incoming[key] = {
                "utilization": _percent(raw.get("utilization")),
                "resets_at": _epoch(raw.get("resetsAt")),
                "status": _text(raw.get("status")) or status,
                "observed_at": when,
            }

    # A rejection often names its window at the top level and sends no
    # unifiedWindows at all. That is still a real observation about that
    # window — its status and its reset time — so it is kept.
    if named and named not in incoming:
        incoming[named] = {
            "utilization": _percent(info.get("utilization")),
            "resets_at": _epoch(info.get("resetsAt")),
            "status": status,
            "observed_at": when,
        }

    windows.update(incoming)

    stored_record = {
        "observed_at": when,
        "status": status,
        "rate_limit_type": named,
        "windows": windows,
    }
    _write(stored_record)
    return stored_record


# ── the shape the dashboard reads ───────────────────────────────────────────

def _window_view(key: str, raw: Optional[dict], now: float) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    observed_at = _epoch(raw.get("observed_at"))
    resets_at = _epoch(raw.get("resets_at"))
    age = None if observed_at is None else max(0.0, now - observed_at)
    return {
        "key": key,
        "label": window_label(key),
        "utilization": _stored_percent(raw.get("utilization")),
        "resets_at": resets_at,
        "status": _text(raw.get("status")),
        "observed_at": observed_at,
        "age_sec": age,
        "stale": age is not None and age > STALE_AFTER_SEC,
        # The window rolled over after we last looked: whatever we hold
        # describes a window that no longer exists.
        "expired": resets_at is not None and resets_at <= now,
    }


def snapshot(*, now: Optional[float] = None) -> dict:
    """The usage picture, honest about everything it does not know."""
    at = time.time() if now is None else float(now)
    stored = latest() or {}
    windows = stored.get("windows") if isinstance(stored.get("windows"), dict) else {}

    keys = list(KNOWN_WINDOWS) + [k for k in windows if k not in KNOWN_WINDOWS]
    views = [_window_view(k, windows.get(k), at) for k in keys]

    observed = [v["observed_at"] for v in views if v["observed_at"] is not None]
    newest = max(observed) if observed else None
    age = None if newest is None else max(0.0, at - newest)

    return {
        "measured": newest is not None,
        "observed_at": newest,
        "age_sec": age,
        "stale": age is not None and age > STALE_AFTER_SEC,
        "stale_after_sec": STALE_AFTER_SEC,
        "status": _text(stored.get("status")),
        "windows": views,
    }
