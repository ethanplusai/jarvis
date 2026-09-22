import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_sends_balanced_latency_and_assembles_stream():
    import tts
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"ID3" + b"\x00" * 100)

    async with _client(handler) as c:
        r = await tts.synthesize_chunk("Good evening, sir.", api_key="k", voice_id="v", client=c)
    assert seen["url"] == tts.FISH_TTS_URL
    assert seen["auth"] == "Bearer k"
    assert seen["body"] == {"text": "Good evening, sir.", "reference_id": "v",
                            "format": "mp3", "mp3_bitrate": 128, "latency": "balanced"}
    assert r is not None and r.audio.startswith(b"ID3") and len(r.audio) == 103
    assert r.first_byte_sec >= 0 and r.total_sec >= r.first_byte_sec


@pytest.mark.asyncio
async def test_non_200_returns_none():
    import tts
    async with _client(lambda req: httpx.Response(401, content=b"nope")) as c:
        assert await tts.synthesize_chunk("x", api_key="k", voice_id="v", client=c) is None


@pytest.mark.asyncio
async def test_transport_error_returns_none():
    import tts

    def boom(request):
        raise httpx.ConnectError("down")

    async with _client(boom) as c:
        assert await tts.synthesize_chunk("x", api_key="k", voice_id="v", client=c) is None


@pytest.mark.asyncio
async def test_empty_text_or_missing_key_short_circuits():
    import tts
    calls = []

    async with _client(lambda req: calls.append(1) or httpx.Response(200, content=b"x")) as c:
        assert await tts.synthesize_chunk("   ", api_key="k", voice_id="v", client=c) is None
        assert await tts.synthesize_chunk("hi", api_key="", voice_id="v", client=c) is None
    assert calls == []


@pytest.mark.asyncio
async def test_elevenlabs_sends_voice_model_and_key_and_returns_the_mp3():
    import tts
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("xi-api-key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"ID3mp3")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        r = await tts.synthesize_elevenlabs("Good evening.", api_key="k", voice_id="VOICE",
                                            model_id="eleven_v3", client=client)
    assert r is not None and r.audio == b"ID3mp3"
    assert "/v1/text-to-speech/VOICE" in seen["url"]
    assert seen["key"] == "k"
    assert seen["body"] == {"text": "Good evening.", "model_id": "eleven_v3"}


@pytest.mark.asyncio
async def test_elevenlabs_error_is_none_not_an_exception():
    import tts
    transport = httpx.MockTransport(lambda req: httpx.Response(401, json={"detail": "bad key"}))
    async with httpx.AsyncClient(transport=transport) as client:
        r = await tts.synthesize_elevenlabs("Hello.", api_key="bad", voice_id="V",
                                            model_id="eleven_v3", client=client)
    assert r is None
