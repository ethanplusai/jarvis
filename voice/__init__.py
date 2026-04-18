"""
Voice WebSocket modules — everything the voice_handler calls, plus the
helpers those helpers use (embedded action dispatch, background
lookups, Claude Code dispatch, etc.). Flat layout inside the package
since most of these modules are at the same "tier" of abstraction.
"""

from .chat_mode import handle_chat_message
from .dispatch import execute_prompt_project, execute_research, self_work_and_notify
from .fast_actions import detect_action_fast
from .greeting import maybe_greet
from .lookups import (
    do_calendar_lookup,
    do_mail_lookup,
    do_screen_lookup,
    get_lookup_status,
    lookup_and_report,
)
from .planning import handle_planning_message
from .session_memory import SessionMemory
from .tts import speak, speak_fallback
from .work_mode import handle_work_mode_message

__all__ = [
    "SessionMemory",
    "detect_action_fast",
    "do_calendar_lookup",
    "do_mail_lookup",
    "do_screen_lookup",
    "execute_prompt_project",
    "execute_research",
    "get_lookup_status",
    "handle_chat_message",
    "handle_planning_message",
    "handle_work_mode_message",
    "lookup_and_report",
    "maybe_greet",
    "self_work_and_notify",
    "speak",
    "speak_fallback",
]
