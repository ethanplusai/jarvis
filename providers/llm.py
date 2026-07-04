"""Unified LLM completion across providers.

``complete()`` returns a plain ``str`` for every provider, so the rest of JARVIS
(the ``[ACTION:X]`` parsing pipeline, TTS, etc.) is completely unaware of which
brain produced the text. Non-Anthropic providers use raw ``httpx`` to avoid extra
SDK dependencies and keep one timeout/error policy.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

import httpx

from . import config

log = logging.getLogger("jarvis.llm")

# Reused verbatim from the original generate_response fallback so a provider
# failure sounds identical to the user regardless of which brain is active.
FALLBACK = "Apologies, sir. I'm having trouble connecting to my language systems."

# on_usage(input_tokens, output_tokens) -> None
UsageCB = Optional[Callable[[int, int], None]]


async def complete(
    *,
    provider: str,
    model: str,
    system: str,
    messages: list[dict],
    max_tokens: int,
    anthropic_client=None,
    on_usage: UsageCB = None,
    timeout: float = 60.0,
) -> str:
    """Generate a completion from the active provider. Always returns a string."""
    meta = config.LLM_PROVIDERS.get(provider) or config.LLM_PROVIDERS[config.DEFAULT_LLM_PROVIDER]
    kind = meta["kind"]
    try:
        if kind == "anthropic":
            return await _complete_anthropic(anthropic_client, model, system, messages, max_tokens, on_usage)
        if kind == "openai":
            return await _complete_openai(provider, model, system, messages, max_tokens, on_usage, timeout)
        if kind == "gemini":
            return await _complete_gemini(provider, model, system, messages, max_tokens, on_usage, timeout)
        raise ValueError(f"Unknown provider kind: {kind}")
    except Exception as e:  # noqa: BLE001 — provider failures must never crash the loop
        log.error(f"LLM provider '{provider}' error: {e}")
        return FALLBACK


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------

async def _complete_anthropic(client, model, system, messages, max_tokens, on_usage) -> str:
    if client is None:
        raise RuntimeError("Anthropic client unavailable (no ANTHROPIC_API_KEY)")
    resp = await client.messages.create(
        model=model, max_tokens=max_tokens, system=system, messages=messages,
    )
    if on_usage and hasattr(resp, "usage"):
        on_usage(getattr(resp.usage, "input_tokens", 0), getattr(resp.usage, "output_tokens", 0))
    return resp.content[0].text


def _normalise_model(provider: str, model: str) -> str:
    """Map spoken/legacy aliases to real API model identifiers."""
    wanted = (model or "").strip()
    if provider == "openai" and wanted.lower() in {"gpt-5-5", "gpt-5.5", "gpt 5-5", "gpt 5.5"}:
        return config.LLM_PROVIDERS["openai"]["default_model"]
    if provider == "deepseek" and wanted.lower() in {"deepseek-v4", "deepseek v4 pro", "deepseek-v4-pro"}:
        return "deepseek-v4-pro"
    return wanted


def _token_limit_key(provider: str, model: str) -> str:
    """Newer OpenAI reasoning models require max_completion_tokens."""
    if provider == "openai" and model.startswith(("gpt-5", "o1", "o3", "o4")):
        return "max_completion_tokens"
    return "max_tokens"


async def _complete_openai(provider, model, system, messages, max_tokens, on_usage, timeout) -> str:
    base = config.llm_base_url(provider)
    if not base:
        raise RuntimeError(f"No base URL configured for {provider}")
    headers = {"Content-Type": "application/json"}
    key = config.llm_api_key(provider)
    if config.LLM_PROVIDERS[provider].get("needs_key") and not key:
        raise RuntimeError(f"No API key configured for {provider}")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    resolved_model = _normalise_model(provider, model)
    payload = {
        "model": resolved_model,
        "messages": [{"role": "system", "content": system}]
        + [{"role": m["role"], "content": m["content"]} for m in messages],
        _token_limit_key(provider, resolved_model): max_tokens,
    }
    async with httpx.AsyncClient(timeout=timeout) as http:
        resp = await http.post(f"{base}/chat/completions", headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    usage = data.get("usage") or {}
    if on_usage:
        on_usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return (message.get("content") or "").strip()


async def _complete_gemini(provider, model, system, messages, max_tokens, on_usage, timeout) -> str:
    base = config.LLM_PROVIDERS[provider]["base_url"]
    key = config.llm_api_key(provider)
    if not key:
        raise RuntimeError("No GOOGLE_API_KEY configured")
    contents = [
        {"role": ("model" if m["role"] == "assistant" else "user"), "parts": [{"text": m["content"]}]}
        for m in messages
    ]
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    url = f"{base}/models/{model}:generateContent?key={key}"
    async with httpx.AsyncClient(timeout=timeout) as http:
        resp = await http.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
    if on_usage:
        um = data.get("usageMetadata") or {}
        on_usage(um.get("promptTokenCount", 0), um.get("candidatesTokenCount", 0))
    cands = data.get("candidates") or []
    parts = cands[0].get("content", {}).get("parts", []) if cands else []
    return "".join(p.get("text", "") for p in parts).strip()


# ---------------------------------------------------------------------------
# Helpers for the settings UI
# ---------------------------------------------------------------------------

async def list_ollama_models() -> tuple[list[str], Optional[str]]:
    """Return (model_names, error). Empty list + error string on failure."""
    base = config.ollama_base_url()
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.get(f"{base}/api/tags")
            resp.raise_for_status()
            data = resp.json()
        names = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
        return names, None
    except Exception as e:  # noqa: BLE001
        return [], str(e)[:200]


async def test_provider(provider: str, key: str | None = None) -> dict:
    """Lightweight connectivity check for one LLM provider."""
    meta = config.LLM_PROVIDERS.get(provider)
    if not meta:
        return {"valid": False, "error": "Unknown provider"}
    # Temporarily honor an explicitly supplied key without persisting it.
    import os
    restore = None
    if key and meta.get("env_key"):
        restore = os.environ.get(meta["env_key"])
        os.environ[meta["env_key"]] = key
    try:
        if meta["needs_key"] and not config.llm_api_key(provider):
            return {"valid": False, "error": "No key provided"}
        text = await complete(
            provider=provider,
            model=config.active_llm_model(provider),
            system="Reply with the single word: ok",
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=8,
            timeout=15.0,
        )
        if text and text != FALLBACK:
            return {"valid": True}
        return {"valid": False, "error": "No response from provider"}
    finally:
        if key and meta.get("env_key"):
            if restore is None:
                os.environ.pop(meta["env_key"], None)
            else:
                os.environ[meta["env_key"]] = restore
