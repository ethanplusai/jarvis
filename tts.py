"""
tts.py — Fish Audio synthesis, one request per sentence chunk.

The response is streamed so time-to-first-byte can be measured; the chunk is
returned whole because the browser decodes one complete MP3 per chunk.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger("jarvis.tts")

FISH_TTS_URL = "https://api.fish.audio/v1/tts"


@dataclass
class SynthResult:
    audio: bytes
    first_byte_sec: float
    total_sec: float


async def synthesize_chunk(text: str, *, api_key: str, voice_id: str,
                           client: Optional[httpx.AsyncClient] = None,
                           latency: str = "balanced", timeout: float = 15.0) -> Optional[SynthResult]:
    text = (text or "").strip()
    if not text or not api_key:
        return None
    own = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    t0 = time.monotonic()
    first: Optional[float] = None
    buf = bytearray()
    try:
        async with client.stream(
            "POST", FISH_TTS_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"text": text, "reference_id": voice_id, "format": "mp3", "mp3_bitrate": 128, "latency": latency},
            timeout=timeout,
        ) as resp:
            if resp.status_code != 200:
                log.error(f"TTS {resp.status_code} for {text[:40]!r}")
                return None
            async for part in resp.aiter_bytes():
                if first is None:
                    first = time.monotonic() - t0
                buf.extend(part)
    except (httpx.HTTPError, OSError) as e:
        log.error(f"TTS error: {e}")
        return None
    finally:
        if own:
            try:
                await client.aclose()
            except Exception as e:      # never turn a clean None into an exception
                log.debug(f"TTS client close failed: {e}")
    if not buf:
        return None
    return SynthResult(bytes(buf), first if first is not None else 0.0, time.monotonic() - t0)


ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"


async def synthesize_elevenlabs(text: str, *, api_key: str, voice_id: str, model_id: str,
                                client: Optional[httpx.AsyncClient] = None,
                                timeout: float = 30.0) -> Optional[SynthResult]:
    """ElevenLabs synthesis — same contract as `synthesize_chunk`: one MP3, or None."""
    text = (text or "").strip()
    if not text or not api_key:
        return None
    if not voice_id:
        log.error("ELEVENLABS_API_KEY is set but ELEVENLABS_VOICE_ID is not; no voice")
        return None
    own = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    t0 = time.monotonic()
    first: Optional[float] = None
    buf = bytearray()
    try:
        async with client.stream(
            "POST", ELEVENLABS_TTS_URL.format(voice_id=voice_id),
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": api_key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
            json={"text": text, "model_id": model_id},
            timeout=timeout,
        ) as resp:
            if resp.status_code != 200:
                body = (await resp.aread())[:200]
                log.error(f"ElevenLabs TTS {resp.status_code} for {text[:40]!r}: {body!r}")
                return None
            async for part in resp.aiter_bytes():
                if first is None:
                    first = time.monotonic() - t0
                buf.extend(part)
    except (httpx.HTTPError, OSError) as e:
        log.error(f"ElevenLabs TTS error: {e}")
        return None
    finally:
        if own:
            try:
                await client.aclose()
            except Exception as e:
                log.debug(f"TTS client close failed: {e}")
    if not buf:
        return None
    return SynthResult(bytes(buf), first if first is not None else 0.0, time.monotonic() - t0)
