"""
tts.py — Fish Audio synthesis, one request per sentence chunk.

The response is streamed so time-to-first-byte can be measured; the chunk is
returned whole because the browser decodes one complete MP3 per chunk.

Without a Fish Audio key, Windows has a free fallback: the system's own SAPI
voices (System.Speech), driven through one long-lived PowerShell process so
each chunk does not pay PowerShell's ~1s startup. It returns WAV, which the
browser's decodeAudioData plays as readily as MP3.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger("jarvis.tts")

FISH_TTS_URL = "https://api.fish.audio/v1/tts"
FISH_KEY_PLACEHOLDER = "your-fish-audio-api-key-here"     # what .env.example ships with


def fish_key_usable(key: Optional[str]) -> bool:
    """A key worth sending: set, and not the template's placeholder."""
    key = (key or "").strip()
    return bool(key) and key != FISH_KEY_PLACEHOLDER


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


# ---------------------------------------------------------------------------
# Windows fallback: SAPI through one persistent PowerShell worker
# ---------------------------------------------------------------------------

WINDOWS_VOICE_DEFAULT = "Microsoft David Desktop"

# One line in, one line out, both base64 so no text can break the framing:
# in, the UTF-8 text; out, the WAV, or "ERR <message>". The voice name comes in
# through the environment, never spliced into the script.
_SAPI_WORKER = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$want = $env:JARVIS_WINDOWS_VOICE
if ($want) { try { $synth.SelectVoice($want) } catch { [Console]::Error.WriteLine("voice not found: $want") } }
$utf8 = New-Object System.Text.UTF8Encoding($false)
$in = New-Object System.IO.StreamReader([Console]::OpenStandardInput(), $utf8)
$out = New-Object System.IO.StreamWriter([Console]::OpenStandardOutput(), [System.Text.Encoding]::ASCII)
$out.AutoFlush = $true
$out.WriteLine('READY')
while ($null -ne ($line = $in.ReadLine())) {
    try {
        $text = $utf8.GetString([Convert]::FromBase64String($line))
        $ms = New-Object System.IO.MemoryStream
        $synth.SetOutputToWaveStream($ms)
        $synth.Speak($text)
        $synth.SetOutputToNull()
        $out.WriteLine([Convert]::ToBase64String($ms.ToArray()))
    } catch {
        $out.WriteLine('ERR ' + $_.Exception.Message.Replace("`r", ' ').Replace("`n", ' '))
    }
}
"""


def windows_speech_available() -> bool:
    return sys.platform == "win32"


class WindowsSpeech:
    """Serialises chunks through one PowerShell process, restarting it if it dies."""

    def __init__(self, voice: Optional[str] = None, timeout: float = 15.0):
        self.voice = voice if voice is not None else os.getenv("JARVIS_WINDOWS_VOICE", WINDOWS_VOICE_DEFAULT)
        self.timeout = timeout
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()

    async def _start(self) -> asyncio.subprocess.Process:
        script = base64.b64encode(_SAPI_WORKER.encode("utf-16-le")).decode()
        env = dict(os.environ, JARVIS_WINDOWS_VOICE=self.voice or "")
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-EncodedCommand", script,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            limit=64 * 1024 * 1024,           # one line carries a whole base64 WAV
        )
        line = await asyncio.wait_for(proc.stdout.readline(), self.timeout)
        if line.strip() != b"READY":
            proc.kill()
            raise OSError(f"speech worker did not start: {line[:80]!r}")
        return proc

    async def synthesize(self, text: str) -> Optional[SynthResult]:
        text = (text or "").strip()
        if not text:
            return None
        async with self._lock:
            t0 = time.monotonic()
            try:
                if self._proc is None or self._proc.returncode is not None:
                    self._proc = await self._start()
                self._proc.stdin.write(base64.b64encode(text.encode("utf-8")) + b"\n")
                await self._proc.stdin.drain()
                line = (await asyncio.wait_for(self._proc.stdout.readline(), self.timeout)).strip()
            except (OSError, asyncio.TimeoutError, ValueError) as e:
                log.error(f"Windows TTS error: {e!r}")
                await self.close()
                return None
            if not line or line.startswith(b"ERR"):
                log.error(f"Windows TTS failed for {text[:40]!r}: {line[:200]!r}")
                if not line:
                    await self.close()
                return None
            audio = base64.b64decode(line)
            total = time.monotonic() - t0
            return SynthResult(audio, total, total)

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except (OSError, ProcessLookupError):
                pass
