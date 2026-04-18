"""
Three-tier conversation memory — keeps a short window for the LLM
(`history`), a full buffer of everything said, and a rolling summary
of older turns. Triggers a background Haiku refresh of the summary
every 5 messages once history exceeds the window size.
"""

import asyncio
import logging
from typing import Any

from llm import update_session_summary

log = logging.getLogger("jarvis.session_memory")


class SessionMemory:
    """Per-connection buffer + rolling summary state."""

    def __init__(self, window: int = 20, refresh_every: int = 5) -> None:
        self.buffer: list[dict] = []  # every message, never truncated
        self.summary: str = ""  # rolling summary of older conversation
        self._update_pending: bool = False
        self._messages_since_refresh: int = 0
        self._window = window
        self._refresh_every = refresh_every

    def record(self, user_text: str, assistant_text: str, history: list[dict]) -> None:
        """Append a turn to history + buffer and kick off a summary refresh if due."""
        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": assistant_text})
        self.buffer.append({"role": "user", "content": user_text})
        self.buffer.append({"role": "assistant", "content": assistant_text})
        self._messages_since_refresh += 1

    def maybe_refresh(self, history: list[dict], anthropic_client: Any) -> None:
        """Schedule a background summary refresh if we've accumulated enough turns."""
        if self._messages_since_refresh < self._refresh_every or len(history) <= self._window or self._update_pending:
            return

        rotated = history[: -self._window] if len(history) > self._window else []
        if not rotated or not anthropic_client:
            self._messages_since_refresh = 0
            return

        self._update_pending = True
        self._messages_since_refresh = 0

        async def _do_refresh(_rotated: list[dict] = rotated) -> None:
            self.summary = await update_session_summary(self.summary, _rotated, anthropic_client)
            self._update_pending = False

        asyncio.create_task(_do_refresh())
