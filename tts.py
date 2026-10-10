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

FISH_ORIGIN = "https://api.fish.audio"
FISH_TTS_URL = FISH_ORIGIN + "/v1/tts"
# The service's own default. Paid; `s2.1-pro-free` is the free tier.
DEFAULT_MODEL = "s2.1-pro"


@dataclass
class SynthResult:
    audio: bytes
    first_byte_sec: float
    total_sec: float


# What the request body asks for, per output format. MP3 is what the browser
# decodes; opus (Ogg-encapsulated, 48 kHz mono) is what WhatsApp plays as a
# voice note — see whatsapp.send_voice_note.
FORMATS = {
    "mp3": {"format": "mp3", "mp3_bitrate": 128},
    "opus": {"format": "opus", "opus_bitrate": 32000},
    "wav": {"format": "wav"},
}


async def synthesize_chunk(text: str, *, api_key: str, voice_id: str,
                           client: Optional[httpx.AsyncClient] = None,
                           latency: str = "balanced", timeout: float = 15.0,
                           model: str = DEFAULT_MODEL, fmt: str = "mp3") -> Optional[SynthResult]:
    text = (text or "").strip()
    if not text or not api_key:
        return None
    if fmt not in FORMATS:
        raise ValueError(f"unsupported TTS format {fmt!r}")
    own = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    t0 = time.monotonic()
    first: Optional[float] = None
    buf = bytearray()
    try:
        async with client.stream(
            "POST", FISH_TTS_URL,
            # `model` is a HEADER on this API. Left out, the service picks its
            # paid default and a key on an unfunded account gets 402 for
            # every sentence; `s2.1-pro-free` costs nothing.
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                     "model": model},
            json={"text": text, "reference_id": voice_id, "latency": latency, **FORMATS[fmt]},
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


async def warm(client: httpx.AsyncClient, timeout: float = 3.0) -> bool:
    """Open a connection to the service before there is anything to say.

    The TCP and TLS handshake then overlaps the brain's thinking instead of
    following it. Measured 2026-09-24: a pooled connection idle for longer
    than a few seconds was gone, and the first sentence's first byte paid
    about 0.2 s for a new one. Any response will do, whatever its status;
    what matters is the connection it leaves in the pool. Never raises.
    """
    try:
        await client.get(FISH_ORIGIN + "/", timeout=timeout)
        return True
    except (httpx.HTTPError, OSError) as e:
        log.debug(f"TTS warm-up failed: {e}")
        return False
