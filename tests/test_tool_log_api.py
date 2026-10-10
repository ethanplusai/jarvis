"""The record is reachable, or it is a file nobody will ever open.

A durable log that only a SQLite client can read answers the question
"what did he just do?" only for someone willing to write a query at the
moment they are least inclined to. It gets a route, with the same rules as
the rest of the browser-facing API.
"""

import importlib

import pytest


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import tool_log
    importlib.reload(tool_log)
    import server as server_module
    importlib.reload(server_module)
    run_store.init_db()
    tool_log.init_db()
    return server_module


def _get(server, path="/api/tool-calls"):
    from fastapi.testclient import TestClient
    with TestClient(server.app) as client:
        return client.get(path, headers={"Origin": "http://localhost:5173"})


def test_it_returns_the_decisions_newest_first(wired):
    import tool_log
    tool_log.record(tool="mcp__linkedin__get_feed", server="linkedin",
                    decision="allow", reason="Read-only.")
    tool_log.record(tool="mcp__linkedin__create_post", server="linkedin",
                    decision="deny", reason="waiting for your approval")
    body = _get(wired).json()
    calls = body["calls"]
    assert [c["tool"] for c in calls] == ["mcp__linkedin__create_post",
                                          "mcp__linkedin__get_feed"]
    assert calls[0]["decision"] == "deny"
    assert "approval" in calls[0]["reason"]


def test_it_is_bounded_per_request(wired):
    import tool_log
    for i in range(60):
        tool_log.record(tool=f"mcp__x__t{i}", server="x", decision="allow", reason="r")
    assert len(_get(wired, "/api/tool-calls?limit=10").json()["calls"]) == 10
    assert len(_get(wired, "/api/tool-calls?limit=99999").json()["calls"]) <= 500, \
        "an unbounded limit is a way to ask for the whole table in one breath"


def test_an_empty_log_is_an_empty_list_not_an_error(wired):
    r = _get(wired)
    assert r.status_code == 200 and r.json()["calls"] == []


def test_the_brain_cannot_read_it_with_its_own_token(wired):
    """The tool channel's token authenticates the BRAIN. This is a record OF
    the brain, for the user; handing it back to the thing it describes is
    the wrong direction, and it is not a tool for a reason."""
    from fastapi.testclient import TestClient
    import data_paths
    with TestClient(wired.app) as client:
        r = client.get("/api/tool-calls",
                       headers={"Authorization": f"Bearer {data_paths.ensure_tool_token()}"})
    assert r.status_code in (401, 403), r.status_code
    assert "tool_calls" not in wired.TOOL_HANDLERS
