"""Unified text-to-speech across providers.

``synthesize()`` returns mp3 ``bytes`` (or ``None`` on failure) regardless of which
voice provider is active, so the WebSocket audio path is provider-agnostic. Usage
accounting stays in ``server.synthesize_speech`` which wraps this function.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from . import config

log = logging.getLogger("jarvis.tts")

FISH_API_URL = "https://api.fish.audio/v1/tts"
ELEVEN_API_URL = "https://api.elevenlabs.io/v1/text-to-speech"
ELEVEN_MODEL = "eleven_multilingual_v2"


async def synthesize(text: str) -> Optional[bytes]:
    """Generate speech for the active TTS provider. Returns mp3 bytes or None."""
    provider = config.active_tts_provider()
    if provider == "elevenlabs":
        return await _elevenlabs(text)
    return await _fish(text)


async def _fish(text: str) -> Optional[bytes]:
    key = config.tts_api_key("fish_audio")
    if not key:
        log.warning("FISH_API_KEY not set, skipping TTS")
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.post(
                FISH_API_URL,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "text": text,
                    "reference_id": config.tts_voice_id("fish_audio"),
                    "format": "mp3",
                },
            )
            if resp.status_code == 200:
                return resp.content
            log.error(f"Fish TTS error: {resp.status_code}")
            return None
    except Exception as e:  # noqa: BLE001
        log.error(f"Fish TTS error: {e}")
        return None


async def _elevenlabs(text: str) -> Optional[bytes]:
    key = config.tts_api_key("elevenlabs")
    if not key:
        log.warning("ELEVENLABS_API_KEY not set, skipping TTS")
        return None
    voice_id = config.tts_voice_id("elevenlabs")
    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            resp = await http.post(
                f"{ELEVEN_API_URL}/{voice_id}",
                headers={"xi-api-key": key, "Content-Type": "application/json"},
                params={"output_format": "mp3_44100_128"},
                json={
                    "text": text,
                    "model_id": ELEVEN_MODEL,
                    "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
                },
            )
            if resp.status_code == 200:
                return resp.content
            log.error(f"ElevenLabs TTS error: {resp.status_code} {resp.text[:200]}")
            return None
    except Exception as e:  # noqa: BLE001
        log.error(f"ElevenLabs TTS error: {e}")
        return None


async def test_provider(provider: str, key: str | None = None) -> dict:
    """Lightweight connectivity check for a voice provider."""
    if provider not in config.TTS_PROVIDERS:
        return {"valid": False, "error": "Unknown provider"}
    import os
    meta = config.TTS_PROVIDERS[provider]
    restore = None
    if key:
        restore = os.environ.get(meta["env_key"])
        os.environ[meta["env_key"]] = key
    try:
        if not config.tts_api_key(provider):
            return {"valid": False, "error": "No key provided"}
        # Force the requested provider regardless of the active one.
        audio = await (_elevenlabs("Test.") if provider == "elevenlabs" else _fish("Test."))
        return {"valid": True} if audio else {"valid": False, "error": "No audio returned"}
    finally:
        if key:
            if restore is None:
                os.environ.pop(meta["env_key"], None)
            else:
                os.environ[meta["env_key"]] = restore
