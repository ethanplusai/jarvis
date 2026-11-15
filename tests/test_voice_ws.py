import importlib
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))


class FakeBrain:
    """Stands in for brain.Brain: streams 'Echo: <text>.' in two deltas."""

    def __init__(self):
        self.ready = True
        self.failed = False
        self.turns = []

    async def stop(self):
        pass

    async def turn(self, text, origin="user", on_delta=None, on_tool=None):
        from brain import TurnResult
        self.turns.append((text, origin))
        out = f"Echo: {text}."
        if on_delta:
            on_delta(out[:5])
            on_delta(out[5:])
        return TurnResult(origin=origin, text=out, stop_reason="result", context_tokens=1234,
                          output_tokens=5, duration_sec=0.01, first_delta_sec=0.001)


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    # No ANTHROPIC_* here: preflight warns about a leftover key, and that
    # warning is spoken over the greeting.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server
    importlib.reload(server)
    run_store.init_db()

    async def fake_synth(text):
        return b"MP3:" + text.encode()

    monkeypatch.setattr(server, "_synth_for_speech", fake_synth)

    async def no_checks():
        return []

    # The real checks look at this machine (CLI login, steering, Accessibility)
    # and speak any failure at startup, over the greeting under test.
    monkeypatch.setattr(server.preflight, "run_checks", no_checks)
    # Turns are stamped with the wall clock; the flow tests compare exact
    # text, so the stamp is checked once on its own below.
    monkeypatch.setattr(server, "_with_time", lambda text: text)
    server._last_greeting_time = time.time()          # suppress the greeting by default
    # The voice page's own Origin. /ws/voice refuses any other, because a
    # socket that reaches _handle_utterance speaks with origin="user".
    with TestClient(server.app,
                    headers={"Origin": "http://localhost:5173"}) as c:
        server.brain_instance = FakeBrain()
        yield c, server


def _drain_until(ws, predicate, limit=10):
    seen = []
    for _ in range(limit):
        msg = ws.receive_json()
        seen.append(msg)
        if predicate(msg):
            return seen
    raise AssertionError(f"never saw expected message; got {seen}")


def test_connect_sends_config_and_idle(client):
    c, server = client
    with c.websocket_connect("/ws/voice") as ws:
        assert ws.receive_json() == {"type": "config", "muteMicDuringSpeech": False}
        assert ws.receive_json() == {"type": "status", "state": "idle"}


def test_connect_alone_is_silent(client):
    """The page opens muted and JARVIS does not speak first: only a double
    clap (POST /api/wake) or the user talking starts him."""
    c, server = client
    server._last_greeting_time = 0
    server._wake_state["pending"] = 0.0
    with c.websocket_connect("/ws/voice") as ws:
        assert ws.receive_json()["type"] == "config"
        assert ws.receive_json() == {"type": "status", "state": "idle"}
        # Nothing else is queued: a greeting would already be on its way.
        ws.send_json({"type": "transcript", "text": "hello there", "isFinal": True})
        seen = _drain_until(ws, lambda m: m["type"] == "audio")
        assert seen[-1]["text"] == "Echo: hello there."


def test_a_clap_opened_tab_is_unmuted_and_greeted_through_the_scheduler(client, monkeypatch):
    c, server = client
    import briefing_mcp
    monkeypatch.setattr(briefing_mcp, "weather_brief", lambda: "Clear skies.")
    monkeypatch.setattr(server, "_briefing_due", lambda: False)
    server._last_greeting_time = 0
    server._wake_state["pending"] = time.time()      # the listener just clapped
    with c.websocket_connect("/ws/voice") as ws:
        seen = _drain_until(ws, lambda m: m["type"] == "audio")
        assert {"type": "unmute"} in seen
        first = seen[-1]
        # One utterance, split by sentence: the greeting, then the weather.
        assert first["idx"] == 0 and first["text"].startswith("Good ")
        assert "sir" not in first["text"]
        ws.send_json({"type": "played", "utt": first["utt"], "idx": 0})
        second = _drain_until(ws, lambda m: m["type"] == "audio")[-1]
        assert second["utt"] == first["utt"] and second["text"] == "Clear skies."


def test_transcript_streams_chunks_and_acks_return_to_idle(client):
    c, server = client
    with c.websocket_connect("/ws/voice") as ws:
        ws.receive_json(); ws.receive_json()
        ws.send_json({"type": "transcript", "text": "hello there", "isFinal": True})
        seen = _drain_until(ws, lambda m: m["type"] == "audio")
        assert {"type": "status", "state": "thinking"} in seen
        assert {"type": "status", "state": "speaking"} in seen
        audio = seen[-1]
        assert audio["text"] == "Echo: hello there." and audio["idx"] == 0
        ws.send_json({"type": "played", "utt": audio["utt"], "idx": audio["idx"]})
        seen = _drain_until(ws, lambda m: m == {"type": "status", "state": "idle"})
        assert server.brain_instance.turns == [("hello there", "user")]


def test_echo_of_jarvis_own_words_is_ignored(client):
    c, server = client
    with c.websocket_connect("/ws/voice") as ws:
        ws.receive_json(); ws.receive_json()
        ws.send_json({"type": "transcript", "text": "hello there", "isFinal": True})
        seen = _drain_until(ws, lambda m: m["type"] == "audio")
        # the microphone hears JARVIS: same words come back as a transcript
        ws.send_json({"type": "transcript", "text": "echo hello there", "isFinal": True})
        ws.send_json({"type": "interim", "text": "echo hello"})
        ws.send_json({"type": "transcript", "text": "what is the weather like", "isFinal": True})
        seen = _drain_until(ws, lambda m: m["type"] == "audio" and m["text"] != "Echo: hello there.")
        assert seen[-1]["text"] == "Echo: what is the weather like."
        assert [t for t, _ in server.brain_instance.turns] == ["hello there", "what is the weather like"]


def test_say_that_again_replays_without_a_brain_turn(client):
    c, server = client
    with c.websocket_connect("/ws/voice") as ws:
        ws.receive_json(); ws.receive_json()
        ws.send_json({"type": "transcript", "text": "hello there", "isFinal": True})
        seen = _drain_until(ws, lambda m: m["type"] == "audio")
        audio = seen[-1]
        ws.send_json({"type": "played", "utt": audio["utt"], "idx": audio["idx"]})
        _drain_until(ws, lambda m: m == {"type": "status", "state": "idle"})

        ws.send_json({"type": "transcript", "text": "say that again", "isFinal": True})
        seen = _drain_until(ws, lambda m: m["type"] == "audio")
        replay = seen[-1]
        assert replay["text"] == "Echo: hello there."
        assert replay["data"] == audio["data"], "identical audio bytes, not re-synthesized"
        assert server.brain_instance.turns == [("hello there", "user")], "no brain turn for the replay"


def test_a_real_question_is_not_treated_as_a_replay(client):
    """"What did you mean by that" is a real question and must reach the
    brain, unlike "say that again"."""
    c, server = client
    with c.websocket_connect("/ws/voice") as ws:
        ws.receive_json(); ws.receive_json()
        ws.send_json({"type": "transcript", "text": "hello there", "isFinal": True})
        _drain_until(ws, lambda m: m["type"] == "audio")
        ws.send_json({"type": "transcript", "text": "what did you mean by that", "isFinal": True})
        seen = _drain_until(ws, lambda m: m["type"] == "audio" and m["text"] != "Echo: hello there.")
        assert seen[-1]["text"] == "Echo: what did you mean by that."
        assert [t for t, _ in server.brain_instance.turns] == \
            ["hello there", "what did you mean by that"]


def test_say_that_again_with_nothing_said_yet(client):
    c, server = client
    with c.websocket_connect("/ws/voice") as ws:
        ws.receive_json(); ws.receive_json()
        ws.send_json({"type": "transcript", "text": "say that again", "isFinal": True})
        seen = _drain_until(ws, lambda m: m["type"] == "audio")
        assert seen[-1]["text"] == server.NOTHING_TO_REPLAY_LINE
        assert server.brain_instance.turns == []


def test_speech_corrections_still_apply(client):
    c, server = client
    with c.websocket_connect("/ws/voice") as ws:
        ws.receive_json(); ws.receive_json()
        ws.send_json({"type": "transcript", "text": "open cloud code", "isFinal": True})
        _drain_until(ws, lambda m: m["type"] == "audio")
        assert server.brain_instance.turns[0][0] == "open Claude Code"


def test_brain_not_ready_says_so_instead_of_hanging(client):
    c, server = client
    server.brain_instance.ready = False
    with c.websocket_connect("/ws/voice") as ws:
        ws.receive_json(); ws.receive_json()
        ws.send_json({"type": "transcript", "text": "hello", "isFinal": True})
        seen = _drain_until(ws, lambda m: m["type"] == "audio")
        assert "still starting" in seen[-1]["text"]
        assert server.brain_instance.turns == []


def test_lifespan_builds_brain_without_spawning_under_test(client):
    c, server = client
    # the fixture replaced brain_instance after startup; the one lifespan built must not be running
    assert server.speech is not None
    assert server.brain_instance.turns == []


# ── failure paths (from the Task 7 review) ──────────────────────────────

class ExplodingBrain(FakeBrain):
    async def turn(self, text, origin="user", on_delta=None, on_tool=None):
        self.turns.append((text, origin))
        raise RuntimeError("boom")


def test_a_raising_turn_still_ends_the_utterance_and_speaks_a_recovery_line(client):
    c, server = client
    server.brain_instance = ExplodingBrain()
    with c.websocket_connect("/ws/voice") as ws:
        ws.receive_json(); ws.receive_json()
        ws.send_json({"type": "transcript", "text": "hello", "isFinal": True})
        seen = _drain_until(ws, lambda m: m["type"] == "audio")
        assert "lost my train of thought" in seen[-1]["text"]
        # the failed turn's utterance was closed, not left open: a new turn plays normally
        server.brain_instance = FakeBrain()
        ws.send_json({"type": "played", "utt": seen[-1]["utt"], "idx": seen[-1]["idx"]})
        ws.send_json({"type": "transcript", "text": "still there", "isFinal": True})
        _drain_until(ws, lambda m: m["type"] == "audio" and m["text"] == "Echo: still there.")


def test_voice_emit_raises_for_content_with_no_client_but_not_for_status(client):
    """The scheduler abandons an utterance whose audio reaches nobody (instead of
    waiting forever for an ack); status frames into the void are simply lost."""
    import asyncio
    c, server = client
    assert not server.voice_clients
    with pytest.raises(server.NoVoiceClient):
        asyncio.run(server._voice_emit({"type": "audio", "utt": 1, "idx": 0, "data": "", "text": "x"}))
    with pytest.raises(server.NoVoiceClient):
        asyncio.run(server._voice_emit({"type": "text", "text": "x"}))
    asyncio.run(server._voice_emit({"type": "status", "state": "idle"}))      # no raise
    asyncio.run(server._voice_emit({"type": "stop"}))                          # no raise


def test_a_client_that_dies_mid_send_is_dropped_and_others_still_get_frames(client):
    """A frame now goes on each client's own queue and a writer sends it, so
    deadness is discovered by the writer rather than by the emit. What must
    not change is the outcome: the dead one goes, the live one is served.
    See test_voice_backpressure.py for why the queue is there at all."""
    import asyncio
    c, server = client

    class Dead:
        async def send_json(self, msg):
            raise RuntimeError("gone")

    class Alive:
        def __init__(self):
            self.got = []

        async def send_json(self, msg):
            self.got.append(msg)

    dead, alive = Dead(), Alive()

    async def scenario():
        server._add_voice_client(dead)
        server._add_voice_client(alive)
        await server._voice_emit({"type": "audio", "utt": 1, "idx": 0,
                                  "data": "", "text": "x"})
        async with asyncio.timeout(2):
            while dead in server.voice_clients or not alive.got:
                await asyncio.sleep(0.01)

    try:
        asyncio.run(scenario())
        assert alive.got and dead not in server.voice_clients
        assert alive in server.voice_clients
    finally:
        server.voice_clients.discard(alive)
        server._voice_queues.pop(alive, None)
        server._voice_writers.pop(alive, None)


def test_fmt_reset_names_the_day_when_it_is_not_today(client):
    """A seven-day window resets days away: "until 10 AM" on Monday at 10 AM is
    both wrong and confusing. Found live 2026-09-02."""
    from datetime import datetime, timedelta
    c, server = client
    now = datetime.now()

    def at(days, hour=10):
        return (now.replace(hour=hour, minute=0, second=0, microsecond=0)
                + timedelta(days=days)).timestamp()

    assert server._fmt_reset(at(0)) == "10 AM"
    assert server._fmt_reset(at(1)) == "tomorrow at 10 AM"
    three = server._fmt_reset(at(3))
    assert three.endswith(" at 10 AM") and three.split(" at ")[0].isalpha()
    far = server._fmt_reset(at(9))
    assert far.endswith(" at 10 AM") and any(ch.isdigit() for ch in far.split(" at ")[0])
    assert server._fmt_reset(None) == "later"
    assert server._fmt_reset("not a time") == "later"


def test_a_user_turn_is_stamped_with_the_local_time():
    import importlib
    import server
    importlib.reload(server)
    stamped = server._with_time("hello there")
    assert stamped.startswith("[") and "local time] hello there" in stamped
