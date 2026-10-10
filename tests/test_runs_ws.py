import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server
    importlib.reload(server)
    run_store.init_db()
    # The dashboard's own Origin: /ws/runs refuses a handshake from a page
    # JARVIS does not serve. See test_web_security.py.
    with TestClient(server.app,
                    headers={"Origin": "http://localhost:5173"}) as c:
        yield c, server, run_store


def test_ws_runs_accepts_connection(client):
    c, _, _ = client
    with c.websocket_connect("/ws/runs") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"


def test_ws_runs_forwards_published_messages(client):
    c, server, store = client
    with c.websocket_connect("/ws/runs") as ws:
        ws.receive_json()  # hello
        run_id = store.create_run("x", "p", "/tmp/p", "api")
        server.run_executor_instance._publish(
            {"type": "run_updated", "run": store.get_run(run_id)})
        msg = ws.receive_json()
        assert msg["type"] == "run_updated"
        assert msg["run"]["id"] == run_id


def test_ws_runs_unsubscribes_on_disconnect(client):
    c, server, _ = client
    before = len(server.run_executor_instance._subscribers)
    with c.websocket_connect("/ws/runs") as ws:
        ws.receive_json()
        assert len(server.run_executor_instance._subscribers) == before + 1
    assert len(server.run_executor_instance._subscribers) == before


def test_voice_ws_untouched_by_runs_ws(client):
    c, server, _ = client
    # /ws/runs must own its subscription and nothing else. The old
    # task_manager._websockets registry is gone, so the observable
    # shared state is the executor's subscriber list plus the route table.
    routes_before = [getattr(r, "path", type(r).__name__) for r in server.app.routes]
    subs_before = len(server.run_executor_instance._subscribers)

    for _ in range(3):
        with c.websocket_connect("/ws/runs") as ws:
            ws.receive_json()  # hello
            # while /ws/runs is open, exactly one subscriber was added
            assert len(server.run_executor_instance._subscribers) == subs_before + 1
        assert len(server.run_executor_instance._subscribers) == subs_before

    assert [getattr(r, "path", type(r).__name__) for r in server.app.routes] == routes_before
    assert "/ws/voice" in routes_before
