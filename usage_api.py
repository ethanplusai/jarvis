"""Usage, over HTTP: the voice path's own call log and the CLI's rate-limit
windows and transcript totals. Carved out of server.py — one surface, one
module, mounted with `app.include_router`. `_append_usage_entry` and
`_session_tokens` are written by the TTS path in server.py, which imports
them from here (the dict is shared by reference)."""
import asyncio
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import data_paths
import usage_scan
import usage_store

log = logging.getLogger("jarvis")
router = APIRouter()

# Which sessions are JARVIS's own runs (so the transcript totals can tell
# them from the user's conversations). The cached answer and its reset live
# in server.py — tests reset `server._run_ids_cache` — so server plugs its
# function in here after import; until then nothing is filtered.
own_session_ids = lambda: frozenset()   # noqa: E731  (replaced by server.py)


# Usage tracking — logs every call with timestamp, persists to disk.
#
# Under `data_paths.data_dir()` like everything else JARVIS writes, and
# computed per call rather than at import: the old module-level
# `Path(__file__).parent / "data"` ignored JARVIS_DATA_DIR, so the README's
# scratch-instance recipe appended to the REAL install's log.
def _usage_file() -> Path:
    return data_paths.usage_log_path()


_session_start = time.time()
_session_tokens = {"input": 0, "output": 0, "api_calls": 0, "tts_calls": 0}


def _append_usage_entry(input_tokens: int, output_tokens: int, call_type: str = "api"):
    """Append a usage entry with timestamp to the log file."""
    try:
        usage_file = _usage_file()
        import json as _json
        entry = {
            "ts": time.time(),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "type": call_type,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        with open(usage_file, "a", encoding="utf-8") as f:
            f.write(_json.dumps(entry) + "\n")
    except Exception:
        pass


def _get_usage_for_period(seconds: float | None = None) -> dict:
    """Sum usage from the log file for a time period. None = all time."""
    import json as _json
    totals = {"input_tokens": 0, "output_tokens": 0, "api_calls": 0, "tts_calls": 0}
    cutoff = (time.time() - seconds) if seconds else 0
    try:
        usage_file = _usage_file()
        if usage_file.exists():
            for line in usage_file.read_text(encoding="utf-8").strip().split("\n"):
                if not line:
                    continue
                entry = _json.loads(line)
                if entry["ts"] >= cutoff:
                    totals["input_tokens"] += entry.get("input_tokens", 0)
                    totals["output_tokens"] += entry.get("output_tokens", 0)
                    if entry.get("type") == "tts":
                        totals["tts_calls"] += 1
                    else:
                        totals["api_calls"] += 1
    except Exception:
        pass
    return totals






@router.get("/api/usage")
async def api_usage():
    uptime = int(time.time() - _session_start)
    today = _get_usage_for_period(86400)
    week = _get_usage_for_period(86400 * 7)
    month = _get_usage_for_period(86400 * 30)
    all_time = _get_usage_for_period(None)
    # Tokens and calls only. This used to attach a `cost_usd` priced at a
    # hard-coded rate per million tokens — on a subscription with no spend
    # to report (usage_store.py says why), that was a number made up.
    return {
        "session": {**_session_tokens, "uptime_seconds": uptime},
        "today": today,
        "week": week,
        "month": month,
        "all_time": all_time,
    }


@router.get("/api/usage/limits")
async def api_usage_limits():
    """How much of the subscription's windows is gone, and when we last looked.

    JARVIS bills nobody — it runs on the user's Claude subscription — so the
    honest headline number is utilisation against the five-hour and seven-day
    limits, not dollars. The reading comes from the CLI's rate_limit_event and
    only exists once the brain has taken a turn, so `measured: false` and
    `utilization: null` are normal answers, not errors. See usage_store.py.
    """
    return usage_store.snapshot()


# --- Per-session usage -----------------------------------------------------
#
# `/api/usage/limits` above is the SUBSCRIPTION's picture: how much of the
# five-hour and seven-day windows is gone. It knows nothing about who spent
# it. That question is only answerable from the CLI's own transcripts, and
# `usage_scan` is the reader — see its module docstring for the three traps
# on this machine (hardlinked roots, 548 MB of files, subagents in their own
# folder).
#
# Two things this endpoint owns that the scanner cannot know by itself:
#
#   * the set of run ids, so JARVIS's own one-shot runs are bucketed apart
#     from the user's conversations. `_jarvis_run_session_ids` is the same
#     source `_snapshot_or_empty` uses for the roster, so the Usage tab and
#     the Sessions tab agree about what counts as the user's work.
#   * a TTL. A cold scan is ~3 s of disk; a warm one is ~46 ms, measured.
#     Every open dashboard polls this, so the answer is held briefly and the
#     scan runs off the event loop.

# How long a scan's answer stands before the disk is consulted again. Long
# enough that several tabs polling cost one scan; short enough that a run
# which just finished shows up on the next refresh.
USAGE_SCAN_TTL_SEC = 20.0

# The incremental cursor. Held for the life of the process on purpose: it is
# what turns a 3-second scan into a 46-millisecond one.
_usage_scan_cache = usage_scan.Cache()
_usage_scan_lock = threading.Lock()
_usage_scan_result: tuple[float, dict] = (0.0, {})


def _usage_scan_snapshot() -> dict:
    """The cached per-session reading. Runs on a worker thread."""
    global _usage_scan_result
    with _usage_scan_lock:
        stamped, body = _usage_scan_result
        now = time.time()
        if body and now - stamped < USAGE_SCAN_TTL_SEC:
            return body
        fresh = usage_scan.snapshot(
            cache=_usage_scan_cache,
            own_session_ids=own_session_ids())
        _usage_scan_result = (now, fresh)
        return fresh


@router.get("/api/usage/sessions")
async def api_usage_sessions():
    """What each conversation on this machine has spent.

    A failure here is answered AS a failure. Serving `measured: false` with a
    200 would be indistinguishable from a machine that has never been used,
    and the entire point of this surface is that those two are different.
    """
    try:
        return await asyncio.to_thread(_usage_scan_snapshot)
    except Exception as e:
        log.warning("usage scan failed", exc_info=True)
        return JSONResponse(status_code=503, content={
            "measured": False, "sessions": [], "daily": [],
            "error": f"could not read the transcripts: {e}",
        })
