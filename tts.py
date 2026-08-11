"""
JARVIS macOS speech synthesis — a free, offline alternative to Fish Audio.

macOS ships voices for every language JARVIS offers, so a user without a Fish
Audio key still gets a spoken assistant rather than a silent one. The voice is
plainer than the Fish JARVIS model; the trade is availability and cost.

Voices are discovered from `say -v ?` at runtime rather than hardcoded, since
which ones are installed varies by machine and macOS version.
"""

import asyncio
import logging
import re
import tempfile
from pathlib import Path

log = logging.getLogger("jarvis.tts")

# WAV, which every browser's decodeAudioData accepts. The `say` default is
# AIFF-C, which Chrome does not reliably decode.
_DATA_FORMAT = "LEI16@22050"

# Preferred voice per language, used when installed. Everything else falls back
# to the first voice matching the language. Daniel is the British voice, which
# suits the butler better than the American default.
_PREFERRED = {
    "en": ["Daniel", "Oliver", "Serena"],
    "it": ["Alice", "Federica"],
    "es": ["Monica", "Mónica", "Jorge"],
    "fr": ["Thomas", "Audrey"],
    "de": ["Anna", "Markus"],
    "pt": ["Luciana", "Joana"],
    "nl": ["Xander", "Claire"],
    "ja": ["Kyoko", "Otoya"],
    "zh": ["Tingting", "Tingting"],
}

_voice_cache: list[dict] | None = None

# `say` interprets [[...]] as embedded speech commands — [[volm 0]] silences it,
# among others. The text reaching here came from a speech transcript by way of
# an LLM, so strip the brackets rather than trust that none appear.
_SAY_COMMAND = re.compile(r"\[\[.*?\]\]")


def _parse_voice_line(line: str) -> dict | None:
    """Parse one `say -v ?` row: name, locale tag, then a `#` sample phrase.

    Anchored on the `#`, because the column spacing is not consistent: classic
    voices are padded into a column ("Alice           it_IT   # ...") while
    newer ones carry a parenthesised language in the name and get a single
    space ("Eddy (Italiano (Italia)) it_IT   # ..."). Keying on run length
    silently dropped every voice of the second kind.
    """
    match = re.match(r"^(.+?)\s+([a-z]{2}_[A-Z]{2})\s+#", line)
    if not match:
        return None
    return {"name": match.group(1).strip(), "lang": match.group(2)}


async def list_voices(refresh: bool = False) -> list[dict]:
    """Every installed macOS voice, as {"name", "lang"}."""
    global _voice_cache
    if _voice_cache is not None and not refresh:
        return _voice_cache
    try:
        proc = await asyncio.create_subprocess_exec(
            "say", "-v", "?",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except Exception as e:
        log.warning(f"Could not list voices: {e}")
        return []

    voices = []
    for line in stdout.decode(errors="replace").split("\n"):
        parsed = _parse_voice_line(line)
        if parsed:
            voices.append(parsed)
    _voice_cache = voices
    return voices


async def resolve_voice(speech_lang: str, configured: str = "") -> str | None:
    """Pick the voice to speak with, or None when nothing matches.

    An explicitly configured voice wins outright — including one whose language
    differs from the interface, which is a legitimate thing to want.
    """
    if configured.strip():
        return configured.strip()

    voices = await list_voices()
    if not voices:
        return None

    lang = speech_lang.split("-")[0].lower()
    matching = [v for v in voices if v["lang"].lower().startswith(lang)]
    if not matching:
        return None

    names = {v["name"] for v in matching}
    for preferred in _PREFERRED.get(lang, []):
        if preferred in names:
            return preferred
    return matching[0]["name"]


async def speak(text: str, voice: str | None = None, timeout: float = 30) -> bytes | None:
    """Render text to WAV bytes with the macOS speech synthesiser."""
    clean = _SAY_COMMAND.sub("", text).strip()
    if not clean:
        return None

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "speech.wav"
        # Arguments are passed directly to exec, never through a shell, so the
        # text cannot break out into a command.
        args = ["say", "--data-format=" + _DATA_FORMAT, "-o", str(out)]
        if voice:
            args += ["-v", voice]
        args += ["--", clean]

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            if proc.returncode != 0:
                log.warning(f"say failed: {stderr.decode(errors='replace')[:200]}")
                return None
            if not out.exists():
                log.warning("say produced no output file")
                return None
            return out.read_bytes()
        except asyncio.TimeoutError:
            log.warning("say timed out")
            return None
        except FileNotFoundError:
            log.warning("say is unavailable — macOS TTS needs macOS")
            return None
        except Exception as e:
            log.warning(f"say error: {e}")
            return None
