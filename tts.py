"""
Text-to-speech via Fish Audio API.

Uses the JARVIS voice model (configurable via FISH_VOICE_ID env var).
Returns raw MP3 bytes or None on failure.
"""

import logging
import os

import httpx

from usage import SESSION_TOKENS, append_usage_entry

log = logging.getLogger("jarvis.tts")

FISH_API_KEY = os.getenv("FISH_API_KEY", "")
FISH_VOICE_ID = os.getenv("FISH_VOICE_ID", "612b878b113047d9a770c069c8b4fdfe")  # JARVIS (MCU)
FISH_API_URL = "https://api.fish.audio/v1/tts"


async def synthesize_speech(text: str) -> bytes | None:
    """Generate speech audio from text using Fish Audio TTS."""
    if not FISH_API_KEY:
        log.warning("FISH_API_KEY not set, skipping TTS")
        return None

    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            response = await http.post(
                FISH_API_URL,
                headers={
                    "Authorization": f"Bearer {FISH_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "text": text,
                    "reference_id": FISH_VOICE_ID,
                    "format": "mp3",
                },
            )
            if response.status_code == 200:
                SESSION_TOKENS["tts_calls"] += 1
                append_usage_entry(0, 0, "tts")
                return response.content
            log.error(f"TTS error: {response.status_code}")
            return None
    except Exception as e:
        log.error(f"TTS error: {e}")
        return None
