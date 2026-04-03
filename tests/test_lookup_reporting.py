import asyncio
import logging

import server


class _FakeWS:
    def __init__(self):
        self.messages = []

    async def send_json(self, payload):
        self.messages.append(payload)


def test_lookup_and_report_discards_stale_results(monkeypatch):
    real_sleep = asyncio.sleep
    ws = _FakeWS()

    async def no_audio(_text):
        return None

    async def fast_sleep(_delay):
        await real_sleep(0)

    async def slow_lookup():
        await real_sleep(0.02)
        return "old result"

    async def fast_lookup():
        await real_sleep(0.001)
        return "new result"

    monkeypatch.setattr(server, "synthesize_speech", no_audio)
    monkeypatch.setattr(server.asyncio, "sleep", fast_sleep)
    server._active_lookups.clear()
    server._latest_lookup_by_type.clear()

    async def run_test():
        task1 = asyncio.create_task(server._lookup_and_report("screen", slow_lookup, ws))
        await real_sleep(0.005)
        task2 = asyncio.create_task(server._lookup_and_report("screen", fast_lookup, ws))
        await asyncio.gather(task1, task2)

    asyncio.run(run_test())

    text_messages = [m["text"] for m in ws.messages if m.get("type") == "text"]
    assert text_messages == ["new result"]


def test_lookup_and_report_base64_encodes_audio(monkeypatch):
    ws = _FakeWS()
    real_sleep = asyncio.sleep

    async def fake_audio(_text):
        return b"abc123"

    async def fast_sleep(_delay):
        await real_sleep(0)

    async def lookup():
        return "spoken result"

    monkeypatch.setattr(server, "synthesize_speech", fake_audio)
    monkeypatch.setattr(server.asyncio, "sleep", fast_sleep)
    server._active_lookups.clear()
    server._latest_lookup_by_type.clear()

    asyncio.run(server._lookup_and_report("screen", lookup, ws))

    audio_messages = [m for m in ws.messages if m.get("type") == "audio"]
    assert len(audio_messages) == 1
    assert audio_messages[0]["data"] == "YWJjMTIz"
    assert audio_messages[0]["text"] == "spoken result"


def test_lookup_and_report_logs_full_result(monkeypatch, caplog):
    ws = _FakeWS()
    real_sleep = asyncio.sleep
    long_result = "This is a much longer screen description that should appear in full in the debug log without being truncated midway through the sentence."

    async def no_audio(_text):
        return None

    async def fast_sleep(_delay):
        await real_sleep(0)

    async def lookup():
        return long_result

    monkeypatch.setattr(server, "synthesize_speech", no_audio)
    monkeypatch.setattr(server.asyncio, "sleep", fast_sleep)
    server._active_lookups.clear()
    server._latest_lookup_by_type.clear()

    with caplog.at_level(logging.INFO):
        asyncio.run(server._lookup_and_report("screen", lookup, ws))

    assert f"Lookup screen complete: {long_result}" in caplog.text
