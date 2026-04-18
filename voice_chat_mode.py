"""
Chat-mode message handler — the default path when planner and
work-mode are both idle. Runs fast keyword detection first, then
falls back to Haiku + embedded [ACTION:*] dispatch.
"""

import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from embedded_actions import default_response_for, is_injected
from embedded_actions import dispatch as dispatch_embedded_action
from fast_action_handlers import handle_fast_action
from fast_actions import detect_action_fast
from formatting import extract_action

log = logging.getLogger("jarvis.voice_chat_mode")


async def handle_chat_message(
    user_text: str,
    *,
    ws: Any,
    work_session: Any,
    history: list[dict],
    voice_state: dict,
    ctx_cache: dict,
    anthropic_client: Any,
    dispatch_registry: Any,
    last_jarvis_response: str,
    session_summary: str,
    generate_response: Callable[..., Coroutine[Any, Any, str]],
    execute_prompt_project: Callable[..., Coroutine[Any, Any, None]],
    self_work_and_notify: Callable[..., Coroutine[Any, Any, None]],
    lookup_and_report: Callable[..., Coroutine[Any, Any, None]],
    do_screen_lookup: Callable[[], Awaitable[str]],
    do_calendar_lookup: Callable[[], Awaitable[str]],
    do_mail_lookup: Callable[[], Awaitable[str]],
) -> str:
    """Run fast-keyword + LLM pipeline for one turn. Returns text for TTS."""
    if action := detect_action_fast(user_text):
        return await handle_fast_action(
            action,
            ws=ws,
            history=history,
            voice_state=voice_state,
            dispatch_registry=dispatch_registry,
            lookup_and_report=lookup_and_report,
            do_screen_lookup=do_screen_lookup,
            do_calendar_lookup=do_calendar_lookup,
            do_mail_lookup=do_mail_lookup,
        )

    if not anthropic_client:
        return "API key not configured."

    response_text = await generate_response(
        user_text,
        last_response=last_jarvis_response,
        session_summary=session_summary,
    )

    clean_response, embedded_action = extract_action(response_text)
    if embedded_action and is_injected(embedded_action, ctx_cache):
        log.warning(
            f"Blocked potentially injected action from untrusted context: [ACTION:{embedded_action['action'].upper()}]"
        )
        embedded_action = None

    if not embedded_action:
        return response_text

    log.info(f"LLM embedded action: {embedded_action}")
    response_text = clean_response
    if not response_text.strip():
        response_text = default_response_for(embedded_action["action"], embedded_action["target"])

    await dispatch_embedded_action(
        embedded_action,
        ws=ws,
        work_session=work_session,
        history=history,
        voice_state=voice_state,
        dispatch_registry=dispatch_registry,
        execute_prompt_project=execute_prompt_project,
        self_work_and_notify=self_work_and_notify,
        lookup_and_report=lookup_and_report,
        do_screen_lookup=do_screen_lookup,
    )
    return response_text
