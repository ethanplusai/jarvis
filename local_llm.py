"""A local LLM, reached over the OpenAI-compatible chat API every local server
speaks: Ollama (`/v1`), LM Studio, llama.cpp's `llama-server`, vLLM.

This is the learning system's own model and nothing else's. The voice path
stays on Claude Code (brain.py); what runs here is the slow, private,
free-to-call thinking that turns a failed run into a lesson, merges lessons
into strategies, and writes and repairs skills. None of it is on the
microphone's critical path, so a slow model costs nothing but patience.

Off unless `JARVIS_LOCAL_LLM_MODEL` names a model: a local server needs one
anyway, and an unset name is the clearest possible "I have not set this up".
Every failure comes back as `LocalLLMError` with a sentence a person can act
on — the engine records it and carries on, it never takes the server down.

httpx is already a dependency; nothing new is added.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"      # Ollama's OpenAI endpoint
DEFAULT_TIMEOUT_SEC = 120.0
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}

# A reply is asked for as JSON and then parsed leniently: small local models
# wrap it in a ```json fence, or say a sentence first, far more often than
# they refuse the format outright.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class LocalLLMError(Exception):
    """The local model could not be reached, or said nothing usable."""


@dataclass
class LocalLLM:
    base_url: str = DEFAULT_BASE_URL
    model: str = ""
    api_key: str = ""
    timeout: float = DEFAULT_TIMEOUT_SEC
    # Injected by tests (httpx.MockTransport); None means a real connection.
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> "LocalLLM":
        try:
            timeout = float(os.getenv("JARVIS_LOCAL_LLM_TIMEOUT", "") or DEFAULT_TIMEOUT_SEC)
        except ValueError:
            timeout = DEFAULT_TIMEOUT_SEC
        return cls(
            base_url=(os.getenv("JARVIS_LOCAL_LLM_URL") or DEFAULT_BASE_URL).rstrip("/"),
            model=(os.getenv("JARVIS_LOCAL_LLM_MODEL") or "").strip(),
            api_key=(os.getenv("JARVIS_LOCAL_LLM_API_KEY") or "").strip(),
            timeout=timeout,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.model)

    def _client(self) -> httpx.AsyncClient:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        # A model on this machine is never reached through the user's HTTP
        # proxy: httpx honours HTTP(S)_PROXY, and loopback is not in most
        # people's NO_PROXY.
        loopback = (urlsplit(self.base_url).hostname or "") in _LOOPBACK
        return httpx.AsyncClient(base_url=self.base_url, headers=headers,
                                 timeout=self.timeout, transport=self.transport,
                                 trust_env=not loopback)

    async def chat(self, messages: list[dict], *, temperature: float = 0.2,
                   max_tokens: int = 1200) -> str:
        """One completion. Returns the assistant's text."""
        if not self.enabled:
            raise LocalLLMError("no local model is configured — set "
                                "JARVIS_LOCAL_LLM_MODEL")
        body = {"model": self.model, "messages": messages,
                "temperature": temperature, "max_tokens": max_tokens,
                "stream": False}
        try:
            async with self._client() as client:
                response = await client.post("/chat/completions", json=body)
        except httpx.HTTPError as e:
            raise LocalLLMError(f"the local model at {self.base_url} is not "
                                f"answering ({type(e).__name__})") from e
        if response.status_code != 200:
            raise LocalLLMError(f"the local model said HTTP {response.status_code}")
        try:
            text = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise LocalLLMError("the local model's reply was not a chat completion") from e
        if not isinstance(text, str) or not text.strip():
            raise LocalLLMError("the local model replied with nothing")
        return text

    async def chat_json(self, system: str, user: str, **kwargs) -> dict:
        """A completion that must be one JSON object."""
        text = await self.chat([{"role": "system", "content": system},
                                {"role": "user", "content": user}], **kwargs)
        return parse_json_object(text)

    async def health(self) -> dict:
        """Whether the server answers and which models it offers. Never raises."""
        if not self.enabled:
            return {"ok": False, "reason": "not configured"}
        try:
            async with self._client() as client:
                response = await client.get("/models")
            models = [m.get("id") for m in response.json().get("data", [])
                      if isinstance(m, dict)]
        except (httpx.HTTPError, ValueError, AttributeError) as e:
            return {"ok": False, "reason": f"unreachable ({type(e).__name__})"}
        return {"ok": response.status_code == 200, "models": models,
                "model_present": self.model in models}


def parse_json_object(text: str) -> dict:
    """The first JSON object in `text`, tolerating fences and preamble."""
    candidates = [m.group(1) for m in _FENCE_RE.finditer(text or "")]
    candidates.append(text or "")
    for candidate in candidates:
        start = candidate.find("{")
        while start != -1:
            try:
                value, _ = json.JSONDecoder().raw_decode(candidate[start:])
            except ValueError:
                start = candidate.find("{", start + 1)
                continue
            if isinstance(value, dict):
                return value
            start = candidate.find("{", start + 1)
    raise LocalLLMError("the local model did not answer in JSON")
