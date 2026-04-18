"""
Fast-action handlers — execute the shortlist of keyword-triggered
actions detected by fast_actions.detect_action_fast.

Each branch returns the response text JARVIS should speak. Background
lookups are fired off via asyncio.create_task so the voice loop stays
responsive.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from action_handlers import handle_open_terminal, handle_show_recent
from formatting import format_mc_decisions_for_voice, format_mc_inbox_for_voice, format_mc_tasks_for_voice
from mc_client import mc_client
from usage import get_usage_summary
from work_mode import session_manager

log = logging.getLogger("jarvis.fast_action_handlers")


async def handle_fast_action(
    action: dict,
    *,
    ws: Any,
    history: list[dict],
    voice_state: dict,
    dispatch_registry: Any,
    lookup_and_report: Callable[..., Coroutine[Any, Any, None]],
    do_screen_lookup: Callable[[], Awaitable[str]],
    do_calendar_lookup: Callable[[], Awaitable[str]],
    do_mail_lookup: Callable[[], Awaitable[str]],
) -> str:
    """Run one of the fast keyword actions and return the text to speak."""
    kind = action["action"]

    if kind == "open_terminal":
        return await handle_open_terminal()

    if kind == "show_recent":
        return await handle_show_recent()

    if kind == "describe_screen":
        asyncio.create_task(lookup_and_report("screen", do_screen_lookup, ws, history=history, voice_state=voice_state))
        return "Taking a look now, sir."

    if kind == "check_calendar":
        asyncio.create_task(
            lookup_and_report("calendar", do_calendar_lookup, ws, history=history, voice_state=voice_state)
        )
        return "Checking your calendar now, sir."

    if kind == "check_mail":
        asyncio.create_task(lookup_and_report("mail", do_mail_lookup, ws, history=history, voice_state=voice_state))
        return "Checking your inbox now, sir."

    if kind == "check_dispatch":
        recent = dispatch_registry.get_most_recent()
        if not recent:
            return "No recent builds on record, sir."
        name = recent["project_name"]
        status = recent["status"]
        if status in ("building", "pending"):
            elapsed = int(time.time() - recent["updated_at"])
            return f"Still working on {name}, sir. Been at it for {elapsed} seconds."
        if status == "completed":
            return recent.get("summary") or f"{name} is complete, sir."
        if status in ("failed", "timeout"):
            return f"{name} ran into problems, sir."
        return f"{name} is {status}, sir."

    if kind == "check_sessions":
        return session_manager.format_for_voice()

    if kind == "check_tasks":
        pending = await mc_client.list_tasks(kanban="not-started", limit=20)
        active = await mc_client.list_tasks(kanban="in-progress", limit=20)
        return format_mc_tasks_for_voice(active + pending)

    if kind == "check_inbox":
        messages = await mc_client.list_inbox(agent="me", status="unread", limit=10)
        return format_mc_inbox_for_voice(messages)

    if kind == "check_decisions":
        decisions = await mc_client.list_decisions(status="pending")
        return format_mc_decisions_for_voice(decisions)

    if kind == "check_usage":
        return get_usage_summary()

    return "Understood, sir."
