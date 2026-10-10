"""Cross-platform and asynchronous edge cases found in the September audit."""
import asyncio
import json
from pathlib import Path

import pytest

import repo_read


@pytest.mark.asyncio
async def test_ripgrep_json_preserves_paths_and_colons_in_matching_text(tmp_path, monkeypatch):
    target = tmp_path / "src" / "note.md"
    target.parent.mkdir()
    target.write_text("key: value: match\n", encoding="utf-8")
    records = [
        {"type": "begin", "data": {}},
        {"type": "match", "data": {"path": {"text": str(target)},
         "line_number": 4, "lines": {"text": "key: value: match\n"}}},
        {"type": "match", "data": {"path": {"text": str(tmp_path / '.env')},
         "line_number": 1, "lines": {"text": "secret match"}}},
        {"type": "summary", "data": {}},
    ]

    class Process:
        returncode = 0
        async def communicate(self):
            return "\n".join(json.dumps(r) for r in records).encode(), b""

    async def spawn(*args, **kwargs):
        assert "--json" in args
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    hits = await repo_read._search_rg(tmp_path, "match", "rg")
    assert hits.lines == ["src/note.md:4: key: value: match"]
    assert hits.found == 1


@pytest.mark.asyncio
async def test_voice_failure_warning_follows_delivery_not_completion_order():
    from speech import SpeechScheduler
    messages = []
    success_ready = asyncio.Event()

    async def synth(text):
        if text == "Four.":
            success_ready.set()
            return b"audio"
        await success_ready.wait()
        return None

    async def emit(message):
        messages.append(message)

    scheduler = SpeechScheduler(synth, emit, pause_after=0)
    await scheduler.start()
    try:
        await scheduler.say("One. Two. Three. Four.")
        async def delivered():
            while not any(m.get("text") == "Four." for m in messages):
                await asyncio.sleep(0.01)
        await asyncio.wait_for(delivered(), 3)
        assert [m["text"] for m in messages if m["type"] in {"audio", "text"}] == [
            "One.", "Two.", "Three.", "My voice is failing, sir.", "Four."]
    finally:
        await scheduler.stop()
