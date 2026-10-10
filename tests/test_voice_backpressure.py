"""One client that stops reading must not close JARVIS's mouth.

`_voice_emit` and `_broadcast_session_event` used to `await ws.send_json`
once per client, in a loop, with no timeout and no queue — and the speech
scheduler holds `_emit_lock` across that call. So a single socket that
connected and never read its side of the connection stalled every utterance
for every listener: TCP backpressure fills the kernel buffer, the send never
returns, and JARVIS simply stops talking. A dashboard left open on a sleeping
laptop is enough.

/ws/runs already had the answer — a bounded per-client queue that drops its
oldest message rather than block — and these two paths now use it too.
"""

import asyncio
import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

# Any wait here is bounded: a test for "this must not block" that can itself
# block forever is the bug it is meant to catch.
DEADLINE = 2.0


@pytest.fixture
def srv(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server
    importlib.reload(server)
    run_store.init_db()
    with TestClient(server.app,
                    headers={"Origin": "http://localhost:5173"}):
        yield server
    server.voice_clients.clear()
    server.session_clients.clear()


class Wedged:
    """A client that accepted the connection and then stopped reading."""

    def __init__(self):
        self.started = 0

    async def send_json(self, msg):
        self.started += 1
        await asyncio.sleep(3600)


class Reader:
    def __init__(self):
        self.got = []

    async def send_json(self, msg):
        self.got.append(msg)


class Broken:
    async def send_json(self, msg):
        raise RuntimeError("gone")


AUDIO = {"type": "audio", "utt": 1, "idx": 0, "data": "", "text": "x"}


def test_a_wedged_client_does_not_delay_the_frame(srv):
    """The whole point: _voice_emit returns at once, whatever the sockets do."""
    server = srv
    wedged, reader = Wedged(), Reader()

    async def scenario():
        server._add_voice_client(wedged)
        server._add_voice_client(reader)
        await asyncio.wait_for(server._voice_emit(AUDIO), DEADLINE)
        # Hand the writers one turn of the loop to actually send.
        await asyncio.wait_for(_until(lambda: reader.got), DEADLINE)
        return reader.got

    got = asyncio.run(scenario())
    assert len(got) == 1 and got[0]["transcript_id"]
    assert {k: v for k, v in got[0].items() if k != "transcript_id"} == AUDIO


def test_a_wedged_client_does_not_delay_a_session_event(srv):
    server = srv
    wedged, reader = Wedged(), Reader()

    async def scenario():
        server._add_session_client(wedged)
        server._add_session_client(reader)
        await asyncio.wait_for(
            server._broadcast_session_event({"session": "x"}), DEADLINE)
        await asyncio.wait_for(_until(lambda: reader.got), DEADLINE)
        return reader.got

    got = asyncio.run(scenario())
    assert got == [{"type": "event", "session": "x"}]


def test_a_wedged_client_queue_drops_its_oldest_rather_than_grow(srv):
    """Unbounded is the other way to lose: memory, not latency."""
    server = srv
    wedged = Wedged()

    async def scenario():
        queue = server._add_voice_client(wedged)
        for i in range(server.VOICE_QUEUE_MAX + 50):
            await asyncio.wait_for(
                server._voice_emit({"type": "status", "state": str(i)}),
                DEADLINE)
        return queue

    queue = asyncio.run(scenario())
    assert queue.qsize() <= server.VOICE_QUEUE_MAX
    # The newest frame survived; the oldest is what went.
    kept = [queue.get_nowait()["state"] for _ in range(queue.qsize())]
    assert kept[-1] == str(server.VOICE_QUEUE_MAX + 49)
    assert "0" not in kept


def test_a_client_whose_socket_fails_is_dropped(srv):
    """Deadness is discovered by the writer now, not by the emit."""
    server = srv
    broken, reader = Broken(), Reader()

    async def scenario():
        server._add_voice_client(broken)
        server._add_voice_client(reader)
        await asyncio.wait_for(server._voice_emit(AUDIO), DEADLINE)
        await asyncio.wait_for(
            _until(lambda: broken not in server.voice_clients), DEADLINE)

    asyncio.run(scenario())
    assert len(reader.got) == 1 and reader.got[0]["transcript_id"]
    assert {k: v for k, v in reader.got[0].items() if k != "transcript_id"} == AUDIO
    assert broken not in server.voice_clients
    assert reader in server.voice_clients


def test_a_content_frame_with_only_dropped_clients_still_raises(srv):
    """The scheduler must still learn that audio reached nobody, or it waits
    out its ack timeout for an ack that can never come."""
    server = srv

    async def scenario():
        with pytest.raises(server.NoVoiceClient):
            await asyncio.wait_for(server._voice_emit(AUDIO), DEADLINE)
        # Status frames into the void stay silent.
        await asyncio.wait_for(
            server._voice_emit({"type": "status", "state": "idle"}), DEADLINE)

    asyncio.run(scenario())


def test_dropping_a_client_stops_its_writer(srv):
    """A cancelled writer, not a task leaked per connection."""
    server = srv
    reader = Reader()

    async def scenario():
        server._add_voice_client(reader)
        task = server._voice_writers[reader]
        server._drop_voice_client(reader)
        await asyncio.wait_for(_until(task.done), DEADLINE)
        return task

    task = asyncio.run(scenario())
    assert task.cancelled() or task.done()
    assert reader not in server.voice_clients


async def _until(predicate, step: float = 0.01):
    while not predicate():
        await asyncio.sleep(step)
