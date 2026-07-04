"""Provider metadata and live environment readers.

Single source of truth shared by the router (``providers.llm`` / ``providers.tts``)
and the settings UI. All readers hit ``os.environ`` on each call so values saved at
runtime via ``/api/settings`` take effect immediately.
"""

from __future__ import annotations

import os

from integrations import is_configured

# ---------------------------------------------------------------------------
# LLM providers
# ---------------------------------------------------------------------------
# kind:
#   "anthropic" — native Anthropic SDK (default brain, drives actions/tools)
#   "openai"    — OpenAI-compatible /chat/completions (OpenAI, Ollama, DeepSeek)
#   "gemini"    — Google Generative Language API
LLM_PROVIDERS: dict[str, dict] = {
    "anthropic": {
        "label": "Anthropic Claude",
        "kind": "anthropic",
        "env_key": "ANTHROPIC_API_KEY",
        "needs_key": True,
        "default_model": "claude-haiku-4-5-20251001",
        "models": ["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-6"],
        "base_url": None,
    },
    "openai": {
        "label": "OpenAI",
        "kind": "openai",
        "env_key": "OPENAI_API_KEY",
        "needs_key": True,
        # Official API model IDs verified against OpenAI docs. If users say
        # "GPT 5-5"/"GPT-5.5", keep them on the latest configured GPT-5 family
        # model rather than sending an unsupported alias to the API.
        "default_model": "gpt-5.2",
        "models": ["gpt-5.2", "gpt-5.2-pro", "gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-4.1"],
        "base_url": "https://api.openai.com/v1",
    },
    "ollama": {
        "label": "Ollama (local)",
        "kind": "openai",
        "env_key": "",  # no API key — local
        "needs_key": False,
        "default_model": "llama3.2",
        "models": [],  # discovered at runtime from /api/tags
        "base_url": None,  # derived from OLLAMA_BASE_URL
    },
    "google": {
        "label": "Google Gemini",
        "kind": "gemini",
        "env_key": "GOOGLE_API_KEY",
        "needs_key": True,
        "default_model": "gemini-2.0-flash",
        "models": ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-2.5-pro", "gemini-1.5-flash"],
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
    },
    "deepseek": {
        "label": "DeepSeek",
        "kind": "openai",
        "env_key": "DEEPSEEK_API_KEY",
        "needs_key": True,
        # DeepSeek V4 Pro is the premium/default model; v4-flash is the fast
        # fallback. Legacy chat/reasoner aliases are kept selectable until their
        # published retirement window for users with older configs.
        "default_model": "deepseek-v4-pro",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat", "deepseek-reasoner"],
        "base_url": "https://api.deepseek.com",
    },
}

DEFAULT_LLM_PROVIDER = "anthropic"

# ---------------------------------------------------------------------------
# TTS providers
# ---------------------------------------------------------------------------
TTS_PROVIDERS: dict[str, dict] = {
    "fish_audio": {
        "label": "Fish Audio",
        "env_key": "FISH_API_KEY",
        "voice_key": "FISH_VOICE_ID",
        "default_voice": "612b878b113047d9a770c069c8b4fdfe",
    },
    "elevenlabs": {
        "label": "ElevenLabs",
        "env_key": "ELEVENLABS_API_KEY",
        "voice_key": "ELEVENLABS_VOICE_ID",
        "default_voice": "JBFqnCBsd6RMkjVDRZzb",  # "George" — calm British narration
    },
}

DEFAULT_TTS_PROVIDER = "fish_audio"


# ---------------------------------------------------------------------------
# Live readers
# ---------------------------------------------------------------------------

def model_env_key(provider_id: str) -> str:
    """Env var that overrides the model for a given provider."""
    return f"JARVIS_LLM_MODEL_{provider_id.upper()}"


def active_llm_provider() -> str:
    pid = os.getenv("JARVIS_LLM_PROVIDER", DEFAULT_LLM_PROVIDER).strip()
    return pid if pid in LLM_PROVIDERS else DEFAULT_LLM_PROVIDER


def active_llm_model(provider_id: str | None = None) -> str:
    pid = provider_id or active_llm_provider()
    meta = LLM_PROVIDERS.get(pid, LLM_PROVIDERS[DEFAULT_LLM_PROVIDER])
    return os.getenv(model_env_key(pid), "").strip() or meta["default_model"]


def active_tts_provider() -> str:
    pid = os.getenv("JARVIS_TTS_PROVIDER", DEFAULT_TTS_PROVIDER).strip()
    return pid if pid in TTS_PROVIDERS else DEFAULT_TTS_PROVIDER


def ollama_base_url() -> str:
    """Root URL of the local Ollama server (no trailing slash)."""
    return os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip().rstrip("/")


def llm_base_url(provider_id: str) -> str | None:
    """OpenAI-compatible base URL for a provider (None for non-HTTP kinds)."""
    meta = LLM_PROVIDERS.get(provider_id)
    if not meta:
        return None
    if provider_id == "ollama":
        return f"{ollama_base_url()}/v1"
    return meta.get("base_url")


def llm_api_key(provider_id: str) -> str:
    meta = LLM_PROVIDERS.get(provider_id) or {}
    return os.getenv(meta.get("env_key", ""), "").strip() if meta.get("env_key") else ""


def llm_configured(provider_id: str) -> bool:
    meta = LLM_PROVIDERS.get(provider_id)
    if not meta:
        return False
    if not meta["needs_key"]:
        return True  # local Ollama — assumed reachable
    return is_configured(os.getenv(meta["env_key"], ""))


def tts_api_key(provider_id: str) -> str:
    meta = TTS_PROVIDERS.get(provider_id) or {}
    return os.getenv(meta.get("env_key", ""), "").strip()


def tts_voice_id(provider_id: str) -> str:
    meta = TTS_PROVIDERS.get(provider_id) or {}
    return os.getenv(meta.get("voice_key", ""), "").strip() or meta.get("default_voice", "")


def tts_configured(provider_id: str) -> bool:
    meta = TTS_PROVIDERS.get(provider_id)
    if not meta:
        return False
    return is_configured(os.getenv(meta["env_key"], ""))


def extra_env_keys() -> set[str]:
    """Env keys (beyond the API_PROVIDERS catalog) the settings API must allow."""
    keys = {"JARVIS_LLM_PROVIDER", "JARVIS_TTS_PROVIDER", "OLLAMA_BASE_URL", "ELEVENLABS_VOICE_ID"}
    keys |= {model_env_key(pid) for pid in LLM_PROVIDERS}
    return keys


# ---------------------------------------------------------------------------
# Status payloads for the settings UI
# ---------------------------------------------------------------------------

def llm_status() -> dict:
    active = active_llm_provider()
    providers = []
    for pid, meta in LLM_PROVIDERS.items():
        providers.append({
            "id": pid,
            "label": meta["label"],
            "configured": llm_configured(pid),
            "needs_key": meta["needs_key"],
            "models": meta["models"],
            "default_model": meta["default_model"],
            "active_model": active_llm_model(pid),
            "is_ollama": pid == "ollama",
        })
    return {
        "providers": providers,
        "active": active,
        "active_model": active_llm_model(active),
        "ollama_base_url": ollama_base_url(),
    }


def tts_status() -> dict:
    active = active_tts_provider()
    providers = []
    for pid, meta in TTS_PROVIDERS.items():
        providers.append({
            "id": pid,
            "label": meta["label"],
            "configured": tts_configured(pid),
            "voice_id": tts_voice_id(pid),
        })
    return {"providers": providers, "active": active}
