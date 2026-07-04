"""Provider routing for JARVIS — multi-LLM brains and multi-voice TTS.

This package is intentionally side-effect free and reads configuration from the
environment *live* on every call, so keys/providers saved at runtime take effect
without a server restart.

- ``providers.config`` — provider metadata + live env readers.
- ``providers.llm``    — unified ``complete()`` across Anthropic, OpenAI-compatible
  (OpenAI / Ollama / DeepSeek) and Google Gemini.
- ``providers.tts``    — unified ``synthesize()`` across Fish Audio and ElevenLabs.
"""

from __future__ import annotations

from . import config, llm, tts

__all__ = ["config", "llm", "tts"]
