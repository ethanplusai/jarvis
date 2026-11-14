"""
tts.py — speech synthesis with the Mac's built-in voice (`say`), one call per
sentence chunk. Free and offline: no Fish Audio key needed.

Returns a complete WAV file per chunk; the browser's decodeAudioData plays WAV
just as it played MP3. Pick a voice with MAC_VOICE in .env (default: Daniel,
a British English voice). `say -v '?'` lists the voices on your Mac.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("jarvis.tts")

MAC_VOICE = os.getenv("MAC_VOICE", "Daniel")
MAC_VOICE_RATE = os.getenv("MAC_VOICE_RATE", "")   # words per minute, e.g. 190


@dataclass
class SynthResult:
    audio: bytes
    first_byte_sec: float
    total_sec: float


async def _run_say(text: str, path: str, voice: Optional[str], timeout: float) -> bool:
    args = ["say"]
    if voice:
        args += ["-v", voice]
    if MAC_VOICE_RATE:
        args += ["-r", MAC_VOICE_RATE]
    args += ["-o", path, "--file-format=WAVE", "--data-format=LEI16@22050", "--", text]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        log.error(f"TTS timed out for {text[:40]!r}")
        return False
    if proc.returncode != 0:
        log.error(f"say failed ({proc.returncode}): {err.decode(errors='replace').strip()[:200]}")
        return False
    return True


async def synthesize_chunk(text: str, *, api_key: str = "", voice_id: str = "",
                           client=None, latency: str = "balanced",
                           timeout: float = 15.0) -> Optional[SynthResult]:
    """Same signature as the old Fish Audio version; api_key/voice_id/client are ignored."""
    text = (text or "").strip()
    if not text:
        return None
    t0 = time.monotonic()
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="jarvis-tts-")
    os.close(fd)
    try:
        ok = await _run_say(text, path, MAC_VOICE, timeout)
        if not ok and MAC_VOICE:
            log.warning(f"Voice {MAC_VOICE!r} failed; falling back to the system default voice")
            ok = await _run_say(text, path, None, timeout)
        if not ok:
            return None
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        log.error(f"TTS error: {e}")
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if not data:
        return None
    elapsed = time.monotonic() - t0
    return SynthResult(data, elapsed, elapsed)
