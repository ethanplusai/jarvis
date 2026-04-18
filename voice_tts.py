"""
WebSocket TTS helpers — one place for "synthesize and send" so the
status/audio/text message shape stays consistent across the voice
handler's three speak-points (main response, fix-self ack, error
fallback).
"""

import base64
import contextlib
import logging
from typing import Any

from formatting import strip_markdown_for_tts
from tts import synthesize_speech

log = logging.getLogger("jarvis.voice_tts")


async def speak(ws: Any, text: str, *, announce_status: bool = True) -> None:
    """Synthesize + send. Falls back to plain text if TTS fails.

    When announce_status is True (default) the client is told to enter
    the 'speaking' state before audio, and 'idle' if audio failed.
    """
    tts_text = strip_markdown_for_tts(text)
    audio = await synthesize_speech(tts_text)
    if announce_status:
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "status", "state": "speaking"})
    if audio:
        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": text})
    else:
        await ws.send_json({"type": "text", "text": text})
        if announce_status:
            with contextlib.suppress(Exception):
                await ws.send_json({"type": "status", "state": "idle"})


async def speak_fallback(ws: Any, text: str) -> None:
    """Error-path speak: send the bytes even if TTS returns nothing, no status.

    The client's audioPlayer.onFinished handler transitions back to idle.
    """
    with contextlib.suppress(Exception):
        audio = await synthesize_speech(text)
        if audio:
            await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": text})
        else:
            await ws.send_json({"type": "audio", "data": "", "text": text})
