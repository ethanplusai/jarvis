"""
Work-mode message handler — routes voice input to an active claude -p
session, detects stalling, auto-opens localhost URLs, and summarizes
the result via Haiku before speaking it back.
"""

import asyncio
import logging
import re
from collections.abc import Callable, Coroutine
from typing import Any

from .action_handlers import execute_browse

log = logging.getLogger("jarvis.voice_work_mode")


_STALL_WORDS = (
    "which option",
    "would you prefer",
    "would you like me to",
    "before I proceed",
    "before proceeding",
    "should I",
    "do you want me to",
    "let me know",
    "please confirm",
    "which approach",
    "what would you",
)

_LOCALHOST_RE = re.compile(r"https?://localhost:\d+")


def _summarize_system_prompt(user_name: str) -> str:
    return (
        f"You are JARVIS reporting to the user ({user_name}). Summarize what happened in 1-2 sentences. "
        "Speak in first person — 'I built', 'I found', 'I set up'. "
        "You are talking TO THE USER, not to a coding tool. "
        "NEVER give instructions like 'go ahead and build' or 'set up the frontend' — those are NOT for the user. "
        "NEVER say 'Claude Code'. NEVER output [ACTION:...] tags. "
        "NEVER read out URLs. No markdown. British precision."
    )


async def handle_work_mode_message(
    user_text: str,
    *,
    ws: Any,
    work_session: Any,
    anthropic_client: Any,
    user_name: str,
    generate_casual_response: Callable[[], Coroutine[Any, Any, str]],
    is_casual: bool,
) -> str:
    """Process one user message in work mode. Returns text for TTS.

    generate_casual_response is a zero-arg closure that calls the chat
    Haiku path with all the usual context (history, projects, etc.).
    """
    if is_casual:
        return await generate_casual_response()

    await ws.send_json({"type": "status", "state": "working"})
    log.info(f"Work mode → claude -p: {user_text[:80]}")

    full_response = await work_session.send(user_text)

    if full_response and anthropic_client:
        is_stalling = any(w in full_response.lower() for w in _STALL_WORDS)
        if is_stalling and work_session._message_count >= 2:
            log.info("Claude Code stalling — pushing to build")
            push_response = await work_session.send(
                "Stop asking questions. Use your best judgment and start building now. "
                "Write the actual code files. Go with the simplest reasonable approach."
            )
            if push_response:
                full_response = push_response

    if localhost_match := _LOCALHOST_RE.search(full_response or ""):
        asyncio.create_task(execute_browse(localhost_match.group(0)))
        log.info(f"Auto-opening {localhost_match.group(0)}")

    if full_response and anthropic_client:
        try:
            summary = await anthropic_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=100,
                system=_summarize_system_prompt(user_name),
                messages=[{"role": "user", "content": f"Claude Code said:\n{full_response[:2000]}"}],
            )
            return summary.content[0].text
        except Exception:
            return full_response[:200]

    return full_response
