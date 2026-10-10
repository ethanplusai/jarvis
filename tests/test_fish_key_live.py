"""A key entered in the settings panel is the key the next sentence uses.

`/api/settings/keys` wrote .env and os.environ, but every synthesis read the
constant `server.py` captured at import, so on a first install the panel
said "saved" and JARVIS went on getting 401 with the placeholder until
somebody restarted the server. And the panel's Test button answered a 402 —
a real key on an account with no credit — with "HTTP 402", which sent the
user back to re-paste a key that was never the problem.
"""

import asyncio
import importlib

import pytest


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FISH_API_KEY", "your-fish-audio-api-key-here")
    monkeypatch.delenv("FISH_VOICE_ID", raising=False)
    import server as server_module
    importlib.reload(server_module)
    return server_module


def test_a_key_saved_through_settings_is_the_key_the_next_sentence_uses(server, monkeypatch):
    seen = {}

    async def capture(text, api_key, voice_id, client, model):
        seen["key"], seen["voice"] = api_key, voice_id
        return None

    monkeypatch.setattr(server.tts, "synthesize_chunk", capture)
    assert server.FISH_API_KEY == "your-fish-audio-api-key-here", "the import-time value"

    server._write_env_key("FISH_API_KEY", "sk-entered-in-the-panel")
    server._write_env_key("FISH_VOICE_ID", "voice-picked-in-the-panel")
    asyncio.run(server._synth_for_speech("Good evening, sir."))

    assert seen == {"key": "sk-entered-in-the-panel", "voice": "voice-picked-in-the-panel"}


def test_the_non_streaming_path_reads_the_live_key_too(server, monkeypatch):
    seen = {}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, json=None):
            seen["auth"] = headers["Authorization"]
            seen["voice"] = json["reference_id"]
            class R:
                status_code = 500
                text = "nope"
                content = b""
            return R()

    monkeypatch.setattr(server.httpx, "AsyncClient", _Client)
    server._write_env_key("FISH_API_KEY", "sk-live")
    asyncio.run(server.synthesize_speech("hello"))
    assert seen["auth"] == "Bearer sk-live"
    assert seen["voice"] == server.FISH_VOICE_ID


def test_the_model_is_sent_as_a_header_and_defaults_to_the_services_own(monkeypatch):
    import httpx
    import tts
    seen = {}

    def handler(request):
        seen["model"] = request.headers.get("model")
        return httpx.Response(200, content=b"")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    asyncio.run(tts.synthesize_chunk("hi", api_key="k", voice_id="v", client=client))
    assert seen["model"] == "s2.1-pro" == tts.DEFAULT_MODEL

    asyncio.run(tts.synthesize_chunk("hi", api_key="k", voice_id="v", client=client,
                                     model="s2.1-pro-free"))
    assert seen["model"] == "s2.1-pro-free"


def test_fish_model_in_the_environment_reaches_every_fish_request(server, monkeypatch):
    seen = {}

    async def capture(text, api_key, voice_id, client, model):
        seen["stream"] = model
        return None

    monkeypatch.setattr(server.tts, "synthesize_chunk", capture)
    monkeypatch.setenv("FISH_MODEL", "s2.1-pro-free")
    asyncio.run(server._synth_for_speech("hello"))
    assert seen["stream"] == "s2.1-pro-free"

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, json=None):
            seen["post"] = headers["model"]
            class R:
                status_code = 200
                content = b""
            return R()

    monkeypatch.setattr(server.httpx, "AsyncClient", _Client)
    asyncio.run(server.api_test_fish(server.KeyTest(key_value="sk")))
    assert seen["post"] == "s2.1-pro-free"
    asyncio.run(server.synthesize_speech("hello"))
    assert seen["post"] == "s2.1-pro-free"

    monkeypatch.delenv("FISH_MODEL")
    asyncio.run(server.api_test_fish(server.KeyTest(key_value="sk")))
    assert seen["post"] == "s2.1-pro", "unset, the service's own default"


def _fish_answering(monkeypatch, server, status):
    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, json=None):
            class R:
                status_code = status
            return R()
    monkeypatch.setattr(server.httpx, "AsyncClient", _Client)


def test_the_key_test_explains_an_account_with_no_credit(server, monkeypatch):
    _fish_answering(monkeypatch, server, 402)
    result = asyncio.run(server.api_test_fish(server.KeyTest(key_value="sk-real")))
    assert result["valid"] is False
    assert "credit" in result["error"].lower()
    assert "402" not in result["error"], "a number is not an explanation"


def test_the_key_test_still_calls_a_bad_key_a_bad_key(server, monkeypatch):
    _fish_answering(monkeypatch, server, 401)
    result = asyncio.run(server.api_test_fish(server.KeyTest(key_value="sk-wrong")))
    assert result == {"valid": False, "error": "Invalid API key"}


def test_the_key_test_accepts_a_working_key(server, monkeypatch):
    _fish_answering(monkeypatch, server, 200)
    assert asyncio.run(server.api_test_fish(server.KeyTest(key_value="sk-ok"))) == {"valid": True}
