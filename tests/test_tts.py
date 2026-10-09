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


def test_the_template_placeholder_is_not_a_usable_key():
    import tts
    assert tts.fish_key_usable("sk-real")
    assert not tts.fish_key_usable("")
    assert not tts.fish_key_usable(None)
    assert not tts.fish_key_usable("  your-fish-audio-api-key-here ")


def _wav(seconds: float, rate: int = 22050) -> bytes:
    import io, wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def test_wav_length_is_read_from_its_header():
    from speech import wav_seconds, ack_floor_seconds, ACK_FLOOR_FACTOR
    assert abs(wav_seconds(_wav(2.0)) - 2.0) < 1e-6
    assert abs(ack_floor_seconds(_wav(2.0)) - 2.0 * ACK_FLOOR_FACTOR) < 1e-6
    assert wav_seconds(b"ID3" + b"\x00" * 100) == 0.0
    assert wav_seconds(b"RIFF\x00\x00\x00\x00WAVE") == 0.0     # no chunks: no floor
    assert wav_seconds(None) == 0.0


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="System.Speech is Windows-only")
async def test_windows_speech_returns_wav_and_survives_its_worker_dying():
    import tts
    from speech import wav_seconds
    w = tts.WindowsSpeech()
    try:
        r = await w.synthesize("Good evening, sir. Café — 100%.")
        assert r is not None and r.audio[:4] == b"RIFF" and wav_seconds(r.audio) > 0.5
        assert await w.synthesize("   ") is None
        w._proc.kill()
        await w._proc.wait()
        r = await w.synthesize("Back again.")
        assert r is not None and r.audio[:4] == b"RIFF"
    finally:
        await w.close()
