"""Approving a connector action permits the retry; it does not send it.

Connector actions ride in the same queue as the business desk's, so the
existing approval UI lists and approves them with no new surface. But the
two are executed in opposite places. A business action is sent BY JARVIS
when you approve it. A connector action is sent by the user's own MCP
server, which JARVIS cannot call — all he can do is stop refusing it the
next time the brain asks.

So approval must not reach `providers.perform`. `provider` would be
`connector:linkedin`, which `identity()` raises on, and the user would be
told their post failed while it sat waiting.
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
    import business_store
    importlib.reload(business_store)
    import server as server_module
    importlib.reload(server_module)
    run_store.init_db()
    business_store.init_db()
    return server_module


def _approve(server, action, approve=True):
    from fastapi.testclient import TestClient
    with TestClient(server.app) as client:
        return client.post(f"/api/business/actions/{action['id']}/decision",
                           headers={"Origin": "http://localhost:5173"},
                           json={"digest": action["digest"], "approve": approve})


def _staged(server, monkeypatch):
    import business_providers
    import business_store

    def never(*a, **kw):
        raise AssertionError("a connector action must never reach a business provider")
    monkeypatch.setattr(business_providers, "perform", never)
    return business_store.propose("connector:linkedin", "mcp__linkedin__create_post",
                                  {"text": "hello", "confirm_post": True})


def test_approving_a_connector_action_marks_it_approved_and_sends_nothing(wired, monkeypatch):
    import business_store
    action = _staged(wired, monkeypatch)
    r = _approve(wired, action)
    assert r.status_code == 200, r.text
    assert business_store.get_action(action["id"])["state"] == "approved"


def test_rejecting_one_still_rejects(wired, monkeypatch):
    import business_store
    action = _staged(wired, monkeypatch)
    assert _approve(wired, action, approve=False).status_code == 200
    assert business_store.get_action(action["id"])["state"] == "rejected"


def test_the_brains_own_token_still_cannot_approve_anything(wired, monkeypatch):
    """The property the business desk already had, which is the whole reason
    the queue is worth reusing: the party that proposes cannot approve."""
    from fastapi.testclient import TestClient
    import business_store
    import data_paths
    action = _staged(wired, monkeypatch)
    with TestClient(wired.app) as client:
        r = client.post(f"/api/business/actions/{action['id']}/decision",
                        headers={"Authorization": f"Bearer {data_paths.ensure_tool_token()}",
                                 "Origin": "http://localhost:5173"},
                        json={"digest": action["digest"], "approve": True})
    assert r.status_code == 403, r.text
    assert business_store.get_action(action["id"])["state"] == "pending"
