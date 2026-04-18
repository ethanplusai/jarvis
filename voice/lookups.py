"""
Background lookup system — runs slow calendar/mail/screen fetches off the
main conversation path and speaks the result back when done.

Dependencies on JARVIS runtime state (ctx_cache, anthropic_client) are
passed in as arguments so this module stays self-contained.
"""

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from formatting import strip_markdown_for_tts
from macos.calendar_access import (
    format_events_for_context,
    format_schedule_summary,
    get_todays_events,
)
from macos.calendar_access import refresh_cache as refresh_calendar_cache
from macos.mail_access import (
    format_unread_summary,
    get_unread_count,
    get_unread_messages,
)
from macos.screen import describe_screen, get_active_windows
from tts import synthesize_speech

log = logging.getLogger("jarvis.lookups")

_active_lookups: dict[str, dict] = {}  # id -> {"type": str, "status": str, "started": float}


async def lookup_and_report(
    lookup_type: str,
    lookup_fn: Callable[[], Awaitable[str]],
    ws: Any,
    history: list[dict] | None = None,
    voice_state: dict | None = None,
) -> None:
    """Run a slow lookup, then speak the result back.

    JARVIS stays conversational — this runs completely off the main path.
    """
    lookup_id = str(uuid.uuid4())[:8]
    _active_lookups[lookup_id] = {
        "type": lookup_type,
        "status": "working",
        "started": time.time(),
    }

    try:
        result_text = await asyncio.wait_for(lookup_fn(), timeout=30)
        _active_lookups[lookup_id]["status"] = "done"

        if voice_state and time.time() - voice_state["last_user_time"] < 3:
            log.info(f"Skipping lookup audio for {lookup_type} — user spoke recently")
        else:
            tts = strip_markdown_for_tts(result_text)
            audio = await synthesize_speech(tts)
            with contextlib.suppress(Exception):
                await ws.send_json({"type": "status", "state": "speaking"})
                if audio:
                    await ws.send_json({"type": "audio", "data": audio, "text": result_text})
                else:
                    await ws.send_json({"type": "text", "text": result_text})
                await ws.send_json({"type": "status", "state": "idle"})

        log.info(f"Lookup {lookup_type} complete: {result_text[:80]}")

        if history is not None:
            history.append({"role": "assistant", "content": f"[{lookup_type} check]: {result_text}"})

    except TimeoutError:
        _active_lookups[lookup_id]["status"] = "timeout"
        with contextlib.suppress(Exception):
            fallback = f"That {lookup_type} check is taking too long, sir. The data may still be syncing."
            audio = await synthesize_speech(fallback)
            await ws.send_json({"type": "status", "state": "speaking"})
            if audio:
                await ws.send_json({"type": "audio", "data": audio, "text": fallback})
            await ws.send_json({"type": "status", "state": "idle"})
    except Exception as e:
        _active_lookups[lookup_id]["status"] = "error"
        log.warning(f"Lookup {lookup_type} failed: {e}")
    finally:
        await asyncio.sleep(60)
        _active_lookups.pop(lookup_id, None)


async def do_calendar_lookup(ctx_cache: dict) -> str:
    """Slow calendar fetch."""
    await refresh_calendar_cache()
    events = await get_todays_events()
    if events:
        ctx_cache["calendar"] = format_events_for_context(events)
    return format_schedule_summary(events)


async def do_mail_lookup(ctx_cache: dict) -> str:
    """Slow mail fetch."""
    unread_info = await get_unread_count()
    if isinstance(unread_info, dict):
        ctx_cache["mail"] = format_unread_summary(unread_info)
        if unread_info["total"] == 0:
            return "Inbox is clear, sir. No unread messages."
        unread_msgs = await get_unread_messages(count=5)
        summary = format_unread_summary(unread_info)
        if unread_msgs:
            top = unread_msgs[:3]
            details = ". ".join(f"{_short_sender(m['sender'])} regarding {m['subject']}" for m in top)
            return f"{summary} Most recent: {details}."
        return summary
    return "Couldn't reach Mail at the moment, sir."


async def do_screen_lookup(anthropic_client: Any) -> str:
    """Screen describe."""
    if anthropic_client:
        return await describe_screen(anthropic_client)
    windows = await get_active_windows()
    if windows:
        apps = {w["app"] for w in windows}
        active = next((w for w in windows if w["frontmost"]), None)
        result = f"You have {', '.join(apps)} open."
        if active:
            result += f" Currently focused on {active['app']}: {active['title']}."
        return result
    return "Couldn't see the screen, sir."


def get_lookup_status() -> str:
    """Get status of active lookups for when user asks 'how's that coming'."""
    if not _active_lookups:
        return ""
    active = [v for v in _active_lookups.values() if v["status"] == "working"]
    if not active:
        return ""
    parts = []
    for lookup in active:
        elapsed = int(time.time() - lookup["started"])
        parts.append(f"{lookup['type']} check ({elapsed}s)")
    return "Currently working on: " + ", ".join(parts)


def _short_sender(sender: str) -> str:
    """Extract just the name from an email sender string."""
    if "<" in sender:
        return sender.split("<")[0].strip().strip('"')
    if "@" in sender:
        return sender.split("@")[0]
    return sender
