"""
Usage and cost tracking for JARVIS.

Logs every API call with timestamp, persists to usage_log.jsonl.
Holds in-memory session totals.
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("jarvis.usage")

USAGE_FILE = Path(__file__).parent / "data" / "usage_log.jsonl"
SESSION_START = time.time()
SESSION_TOKENS = {"input": 0, "output": 0, "api_calls": 0, "tts_calls": 0}


def append_usage_entry(input_tokens: int, output_tokens: int, call_type: str = "api") -> None:
    """Append a usage entry with timestamp to the log file."""
    try:
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": time.time(),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "type": call_type,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        with open(USAGE_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        log.debug(f"Failed to append usage entry: {e}")


def get_usage_for_period(seconds: float | None = None) -> dict:
    """Sum usage from the log file for a time period. None = all time."""
    totals = {"input_tokens": 0, "output_tokens": 0, "api_calls": 0, "tts_calls": 0}
    cutoff = (time.time() - seconds) if seconds else 0
    try:
        if USAGE_FILE.exists():
            for line in USAGE_FILE.read_text().strip().split("\n"):
                if not line:
                    continue
                entry = json.loads(line)
                if entry["ts"] >= cutoff:
                    totals["input_tokens"] += entry.get("input_tokens", 0)
                    totals["output_tokens"] += entry.get("output_tokens", 0)
                    if entry.get("type") == "tts":
                        totals["tts_calls"] += 1
                    else:
                        totals["api_calls"] += 1
    except Exception as e:
        log.debug(f"Failed to read usage log: {e}")
    return totals


def cost_from_tokens(input_t: int, output_t: int) -> float:
    """Compute USD cost from input/output tokens (Haiku pricing)."""
    return (input_t / 1_000_000) * 0.80 + (output_t / 1_000_000) * 4.00


def track_usage(response) -> None:
    """Track token usage from an Anthropic API response."""
    inp = getattr(response.usage, "input_tokens", 0) if hasattr(response, "usage") else 0
    out = getattr(response.usage, "output_tokens", 0) if hasattr(response, "usage") else 0
    SESSION_TOKENS["input"] += inp
    SESSION_TOKENS["output"] += out
    SESSION_TOKENS["api_calls"] += 1
    append_usage_entry(inp, out, "api")


def get_usage_summary() -> str:
    """Get a voice-friendly usage summary with time breakdowns."""
    uptime_min = int((time.time() - SESSION_START) / 60)

    session = SESSION_TOKENS
    today = get_usage_for_period(86400)
    all_time = get_usage_for_period(None)

    session_cost = cost_from_tokens(session["input"], session["output"])
    today_cost = cost_from_tokens(today["input_tokens"], today["output_tokens"])
    all_cost = cost_from_tokens(all_time["input_tokens"], all_time["output_tokens"])

    parts = [f"This session: {uptime_min} minutes, {session['api_calls']} calls, ${session_cost:.2f}."]

    if today["api_calls"] > session["api_calls"]:
        parts.append(f"Today total: {today['api_calls']} calls, ${today_cost:.2f}.")

    if all_time["api_calls"] > today["api_calls"]:
        parts.append(f"All time: {all_time['api_calls']} calls, ${all_cost:.2f}.")

    return " ".join(parts)
