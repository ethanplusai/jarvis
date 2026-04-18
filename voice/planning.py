"""
Planning-mode message handler — drives the TaskPlanner's question/
answer loop, dispatches to claude -p when the plan is confirmed, and
handles "build it now" bypass phrases.
"""

import asyncio
import logging
import os
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from actions import _generate_project_name
from planner import BYPASS_PHRASES

log = logging.getLogger("jarvis.voice_planning")


def is_bypass_phrase(text_lower: str) -> bool:
    """User said 'just do it' / 'skip the rest' / etc. — dispatch with defaults."""
    return any(p in text_lower for p in BYPASS_PHRASES)


async def _dispatch_plan(
    prompt: str,
    *,
    work_session: Any,
    ws: Any,
    history: list[dict],
    voice_state: dict,
    dispatch_registry: Any,
    execute_prompt_project: Callable[..., Coroutine[Any, Any, None]],
) -> None:
    """Turn a completed plan into a Desktop project and dispatch claude -p on it."""
    name = _generate_project_name(prompt)
    path = str(Path.home() / "Desktop" / name)
    os.makedirs(path, exist_ok=True)
    Path(path, "CLAUDE.md").write_text(prompt)
    did = dispatch_registry.register(name, path, prompt[:200])
    asyncio.create_task(
        execute_prompt_project(
            name,
            prompt,
            work_session,
            ws,
            dispatch_id=did,
            history=history,
            voice_state=voice_state,
        )
    )


async def handle_planning_message(
    user_text: str,
    text_lower: str,
    *,
    planner: Any,
    work_session: Any,
    ws: Any,
    history: list[dict],
    voice_state: dict,
    cached_projects: list[dict],
    dispatch_registry: Any,
    execute_prompt_project: Callable[..., Coroutine[Any, Any, None]],
) -> str:
    """Process one user message while the planner is in clarifying-question mode.

    Returns the text JARVIS should speak next. Dispatches the plan as a
    side effect when confirmed or bypassed.
    """
    if is_bypass_phrase(text_lower):
        plan = planner.active_plan
        if plan:
            plan.skipped = True
            for q in plan.pending_questions[plan.current_question_index :]:
                if q.get("default") is not None and q["key"] not in plan.answers:
                    plan.answers[q["key"]] = q["default"]
        prompt = await planner.build_prompt()
        await _dispatch_plan(
            prompt,
            work_session=work_session,
            ws=ws,
            history=history,
            voice_state=voice_state,
            dispatch_registry=dispatch_registry,
            execute_prompt_project=execute_prompt_project,
        )
        planner.reset()
        return "Building it now, sir."

    plan = planner.active_plan
    if plan and plan.confirmed is False and plan.current_question_index >= len(plan.pending_questions):
        result = await planner.handle_confirmation(user_text)
        if result["confirmed"]:
            prompt = await planner.build_prompt()
            await _dispatch_plan(
                prompt,
                work_session=work_session,
                ws=ws,
                history=history,
                voice_state=voice_state,
                dispatch_registry=dispatch_registry,
                execute_prompt_project=execute_prompt_project,
            )
            planner.reset()
            return "On it, sir."
        if result["cancelled"]:
            planner.reset()
            return "Cancelled, sir."
        return result.get("modification_question", "How shall I adjust the plan, sir?")

    result = await planner.process_answer(user_text, cached_projects)
    if result["plan_complete"]:
        return result.get("confirmation_summary", "Ready to build. Shall I proceed, sir?")
    return result.get("next_question", "What else, sir?")
