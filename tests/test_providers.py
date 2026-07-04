"""Unit tests for the multi-provider LLM/TTS router.

Validates per-provider request shapes (via a fake httpx client), config readers,
and the extract_action regression — all without any real API keys.
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import providers.llm as llm
import providers.tts as tts
from providers import config


# --- Fake httpx client ------------------------------------------------------

class _FakeResp:
    def __init__(self, json_data=None, content=b"\x00\x01"):
        self._j = json_data or {}
        self.status_code = 200
        self.content = content
        self.text = ""

    def raise_for_status(self):
        pass

    def json(self):
        return self._j


class _FakeClient:
    captured: dict = {}
    response: _FakeResp = _FakeResp()

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None, params=None):
        _FakeClient.captured = {"url": url, "headers": headers or {}, "json": json, "params": params}
        return _FakeClient.response

    async def get(self, url):
        _FakeClient.captured = {"url": url}
        return _FakeClient.response


def _patch(monkeypatch_target):
    """Swap httpx.AsyncClient on a module with the fake."""
    monkeypatch_target.httpx.AsyncClient = _FakeClient


# --- config -----------------------------------------------------------------

def test_default_active_provider():
    os.environ.pop("JARVIS_LLM_PROVIDER", None)
    assert config.active_llm_provider() == "anthropic"
    assert config.active_llm_model() == "claude-haiku-4-5-20251001"


def test_invalid_provider_falls_back():
    os.environ["JARVIS_LLM_PROVIDER"] = "nonsense"
    try:
        assert config.active_llm_provider() == "anthropic"
    finally:
        os.environ.pop("JARVIS_LLM_PROVIDER", None)


def test_model_override_per_provider():
    os.environ["JARVIS_LLM_MODEL_OPENAI"] = "gpt-5"
    try:
        assert config.active_llm_model("openai") == "gpt-5"
    finally:
        os.environ.pop("JARVIS_LLM_MODEL_OPENAI", None)
    # falls back to default when unset
    assert config.active_llm_model("openai") == "gpt-5.2"


def test_extra_env_keys_cover_selectors():
    keys = config.extra_env_keys()
    assert "JARVIS_LLM_PROVIDER" in keys
    assert "JARVIS_TTS_PROVIDER" in keys
    assert "OLLAMA_BASE_URL" in keys
    assert "ELEVENLABS_VOICE_ID" in keys
    assert "JARVIS_LLM_MODEL_OLLAMA" in keys


def test_ollama_needs_no_key_so_configured():
    assert config.llm_configured("ollama") is True


def test_keyed_provider_unconfigured_without_key():
    os.environ.pop("OPENAI_API_KEY", None)
    assert config.llm_configured("openai") is False


# --- LLM adapters -----------------------------------------------------------

def test_openai_compatible_request_shape():
    _patch(llm)
    _FakeClient.response = _FakeResp({
        "choices": [{"message": {"content": "  hello sir  "}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5},
    })
    os.environ["OPENAI_API_KEY"] = "sk-test"
    seen = {}
    text = asyncio.run(llm.complete(
        provider="openai", model="gpt-5-mini",
        system="SYS", messages=[{"role": "user", "content": "hi"}],
        max_tokens=42, on_usage=lambda i, o: seen.update(i=i, o=o),
    ))
    os.environ.pop("OPENAI_API_KEY", None)
    cap = _FakeClient.captured
    assert cap["url"] == "https://api.openai.com/v1/chat/completions"
    assert cap["headers"]["Authorization"] == "Bearer sk-test"
    assert cap["json"]["model"] == "gpt-5-mini"
    assert cap["json"]["max_completion_tokens"] == 42
    assert cap["json"]["messages"][0] == {"role": "system", "content": "SYS"}
    assert cap["json"]["messages"][1] == {"role": "user", "content": "hi"}
    assert text == "hello sir"          # trimmed
    assert seen == {"i": 3, "o": 5}     # usage propagated


def test_deepseek_v4_pro_request_shape():
    _patch(llm)
    _FakeClient.response = _FakeResp({"choices": [{"message": {"content": "deep ok"}}]})
    os.environ["DEEPSEEK_API_KEY"] = "sk-deep"
    text = asyncio.run(llm.complete(
        provider="deepseek", model="deepseek v4 pro",
        system="S", messages=[{"role": "user", "content": "x"}], max_tokens=10,
    ))
    os.environ.pop("DEEPSEEK_API_KEY", None)
    cap = _FakeClient.captured
    assert cap["url"] == "https://api.deepseek.com/chat/completions"
    assert cap["headers"]["Authorization"] == "Bearer sk-deep"
    assert cap["json"]["model"] == "deepseek-v4-pro"
    assert cap["json"]["max_tokens"] == 10
    assert text == "deep ok"


def test_ollama_uses_base_url_and_no_auth():
    _patch(llm)
    _FakeClient.response = _FakeResp({"choices": [{"message": {"content": "ok"}}]})
    os.environ["OLLAMA_BASE_URL"] = "http://localhost:11434"
    text = asyncio.run(llm.complete(
        provider="ollama", model="llama3.2",
        system="S", messages=[{"role": "user", "content": "x"}], max_tokens=10,
    ))
    os.environ.pop("OLLAMA_BASE_URL", None)
    cap = _FakeClient.captured
    assert cap["url"] == "http://localhost:11434/v1/chat/completions"
    assert "Authorization" not in cap["headers"]
    assert text == "ok"


def test_gemini_request_shape_and_role_mapping():
    _patch(llm)
    _FakeClient.response = _FakeResp({
        "candidates": [{"content": {"parts": [{"text": "world"}]}}],
        "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 4},
    })
    os.environ["GOOGLE_API_KEY"] = "AIza-test"
    text = asyncio.run(llm.complete(
        provider="google", model="gemini-2.0-flash",
        system="SYS",
        messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "prev"}],
        max_tokens=20,
    ))
    os.environ.pop("GOOGLE_API_KEY", None)
    cap = _FakeClient.captured
    assert "models/gemini-2.0-flash:generateContent?key=AIza-test" in cap["url"]
    assert cap["json"]["systemInstruction"]["parts"][0]["text"] == "SYS"
    assert cap["json"]["contents"][0]["role"] == "user"
    assert cap["json"]["contents"][1]["role"] == "model"   # assistant -> model
    assert cap["json"]["generationConfig"]["maxOutputTokens"] == 20
    assert text == "world"


def test_provider_failure_returns_fallback():
    class _Boom(_FakeClient):
        async def post(self, *a, **k):
            raise RuntimeError("network down")
    llm.httpx.AsyncClient = _Boom
    os.environ["OPENAI_API_KEY"] = "sk-test"
    text = asyncio.run(llm.complete(
        provider="openai", model="gpt-5-mini",
        system="S", messages=[{"role": "user", "content": "x"}], max_tokens=10,
    ))
    os.environ.pop("OPENAI_API_KEY", None)
    assert text == llm.FALLBACK


# --- TTS adapters -----------------------------------------------------------

def test_elevenlabs_request_shape():
    _patch(tts)
    _FakeClient.response = _FakeResp(content=b"mp3bytes")
    os.environ["ELEVENLABS_API_KEY"] = "eleven-test"
    os.environ["ELEVENLABS_VOICE_ID"] = "voiceXYZ"
    os.environ["JARVIS_TTS_PROVIDER"] = "elevenlabs"
    audio = asyncio.run(tts.synthesize("hello"))
    for k in ("ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "JARVIS_TTS_PROVIDER"):
        os.environ.pop(k, None)
    cap = _FakeClient.captured
    assert cap["url"].endswith("/v1/text-to-speech/voiceXYZ")
    assert cap["headers"]["xi-api-key"] == "eleven-test"
    assert cap["json"]["text"] == "hello"
    assert audio == b"mp3bytes"


def test_fish_default_when_no_key_returns_none():
    os.environ.pop("FISH_API_KEY", None)
    os.environ["JARVIS_TTS_PROVIDER"] = "fish_audio"
    audio = asyncio.run(tts.synthesize("hello"))
    os.environ.pop("JARVIS_TTS_PROVIDER", None)
    assert audio is None


# --- extract_action regression (Bug B10) ------------------------------------

def test_extract_action_keeps_speech_after_tag():
    import server
    clean, action = server.extract_action("[ACTION:BROWSE] weather today\nLooking that up, sir.")
    assert action == {"action": "browse", "target": "weather today"}
    assert "Looking that up, sir." in clean


def test_extract_action_keeps_speech_before_tag():
    import server
    clean, action = server.extract_action("Right away, sir. [ACTION:OPEN_TERMINAL] ")
    assert action["action"] == "open_terminal"
    assert clean == "Right away, sir."


def test_extract_action_none_when_absent():
    import server
    clean, action = server.extract_action("Just a normal reply, sir.")
    assert action is None
    assert clean == "Just a normal reply, sir."


# --- manual runner (no pytest required) -------------------------------------

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
