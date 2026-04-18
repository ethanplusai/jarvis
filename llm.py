"""
LLM response generation for JARVIS.

Calls Anthropic Haiku with the full system prompt (personality, context,
memories, action tags) and returns the assistant reply.
"""

import logging
from datetime import datetime

import anthropic

from formatting import format_projects_for_prompt
from memory import build_memory_context
from prompts import JARVIS_SYSTEM_PROMPT
from usage import track_usage

log = logging.getLogger("jarvis.llm")


async def generate_response(
    text: str,
    client: anthropic.AsyncAnthropic,
    task_mgr,  # ClaudeTaskManager — avoid circular import
    projects: list[dict],
    conversation_history: list[dict],
    ctx_cache: dict,
    dispatch_registry,  # DispatchRegistry — avoid circular import
    user_name: str,
    project_dir: str,
    last_response: str = "",
    session_summary: str = "",
    lookup_status: str = "",
) -> str:
    """Generate a JARVIS response using Anthropic API.

    All dependencies injected so this module has no server.py coupling.
    """
    now = datetime.now()
    current_time = now.strftime("%A, %B %d, %Y at %I:%M %p")

    weather_info = ctx_cache.get("weather", "Weather data unavailable.")
    screen_ctx = ctx_cache["screen"]
    calendar_ctx = ctx_cache["calendar"]
    mail_ctx = ctx_cache["mail"]

    system = JARVIS_SYSTEM_PROMPT.format(
        current_time=current_time,
        weather_info=weather_info,
        screen_context=screen_ctx or "Not checked yet.",
        calendar_context=calendar_ctx,
        mail_context=mail_ctx,
        active_tasks=task_mgr.get_active_tasks_summary(),
        dispatch_context=dispatch_registry.format_for_prompt(),
        known_projects=format_projects_for_prompt(projects),
        user_name=user_name,
        project_dir=project_dir,
    )
    if lookup_status:
        system += f"\n\nACTIVE LOOKUPS:\n{lookup_status}\nIf asked about progress, report this status."

    # Inject relevant memories and tasks
    memory_ctx = build_memory_context(text)
    if memory_ctx:
        system += f"\n\nJARVIS MEMORY:\n{memory_ctx}"

    # Three-tier memory — rolling summary of earlier conversation
    if session_summary:
        system += f"\n\nSESSION CONTEXT (earlier in this conversation):\n{session_summary}"

    # Self-awareness — remind JARVIS of last response to avoid repetition
    if last_response:
        system += f'\n\nYOUR LAST RESPONSE (do not repeat this):\n"{last_response[:150]}"'

    messages = conversation_history[-20:]
    if not messages or messages[-1].get("content") != text:
        messages = messages + [{"role": "user", "content": text}]

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            system=system,
            messages=messages,
        )
        track_usage(response)
        return response.content[0].text
    except Exception as e:
        log.error(f"LLM error: {e}")
        return "Apologies, sir. I'm having trouble connecting to my language systems."


async def update_session_summary(
    old_summary: str,
    rotated_messages: list[dict],
    client: anthropic.AsyncAnthropic,
) -> str:
    """Background Haiku call to update the rolling session summary."""
    prompt = f"""Update this conversation summary to include the new messages.

Current summary: {old_summary or "(start of conversation)"}

New messages to incorporate:
{chr(10).join(f"{m['role']}: {m['content'][:200]}" for m in rotated_messages)}

Write an updated summary in 2-4 sentences capturing the key topics, decisions, and context. Be concise."""

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        log.warning(f"Summary update failed: {e}")
        return old_summary
