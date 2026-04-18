"""
Time-of-day greeting — spoken once per minute at the start of a voice
session. Owns the 60s dedupe timer so reconnects don't re-greet.
"""

import asyncio
import base64
import contextlib
import logging
import time
from datetime import datetime
from typing import Any

from tts import synthesize_speech

log = logging.getLogger("jarvis.greeting")

_last_greeting_time: float = 0.0


def _greeting_for_hour(hour: int) -> str:
    if hour < 12:
        return "Good morning, sir."
    if hour < 17:
        return "Good afternoon, sir."
    return "Good evening, sir."


def maybe_greet(ws: Any, history: list[dict]) -> None:
    """Fire off a greeting if > 60s since the last one."""
    global _last_greeting_time
    now = time.time()
    if now - _last_greeting_time <= 60:
        return

    _last_greeting_time = now
    greeting = _greeting_for_hour(datetime.now().hour)

    async def _send() -> None:
        with contextlib.suppress(Exception):
            audio = await synthesize_speech(greeting)
            if not audio:
                return
            encoded = base64.b64encode(audio).decode()
            await ws.send_json({"type": "status", "state": "speaking"})
            await ws.send_json({"type": "audio", "data": encoded, "text": greeting})
            history.append({"role": "assistant", "content": greeting})
            log.info(f"JARVIS: {greeting}")
            await ws.send_json({"type": "status", "state": "idle"})

    asyncio.create_task(_send())
