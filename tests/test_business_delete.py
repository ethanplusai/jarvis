"""Deleting a finished approval — from the desk, never from the ledger.

"Delete" hides an approval that can no longer do anything: submitted,
failed, rejected, unknown, or pending/approved past its expiry. It is a
SOFT delete: the row keeps its `deleted` stamp and its audit trail gains a
`deleted` event, the export still carries it, and only the desk, the
briefing's counts and the connector gate's digest lookups stop seeing it.
A live approval — pending, executing, or an unspent allowance — cannot be
deleted; rejecting is the answer for those, and it is recorded.

Like the decision route, the brain's bearer token cannot do this: evidence
of what it asked for is not the brain's to remove.
"""

import time
from contextlib import closing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import business_api as api
import business_providers as providers
import business_store as store


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    for keys in providers.REQUIRED.values():
        for key in keys:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "1" * 32)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "private-test-credential")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15005550006")
    monkeypatch.setenv("JARVIS_OWNER_PHONE", "+15005550007")
    store.init_db()
    monkeypatch.setattr(api, "_closing", False)


def _proposal():
    return api.propose(api.Proposal(provider="twilio", operation="call", payload={"message": "Hello"}))


def _in_state(state):
    action = _proposal()
    if state != "pending":
        action = store.transition(action["id"], action["digest"], "pending", state)
    return action


def _expire(action_id):
    with closing(store.connect()) as conn, conn:
        conn.execute("UPDATE business_actions SET expires=? WHERE id=?", (time.time() - 1, action_id))


def _ids():
    return [a["id"] for a in store.list_actions()]


# --- the store ------------------------------------------------------------------

@pytest.mark.parametrize("state", ["submitted", "failed", "rejected", "unknown"])
def test_a_finished_approval_leaves_the_desk_but_not_the_ledger(state):
    action = _in_state(state)
    before = time.time()

    store.delete_action(action["id"])

    assert action["id"] not in _ids(), "gone from the desk"
    kept = store.get_action(action["id"])
    assert isinstance(kept["deleted"], float) and kept["deleted"] >= before
    assert kept["state"] == state, "the outcome is not rewritten"
    assert store.audit(action["id"])[-1]["event"] == "deleted"
    assert any(f'"id": "{action["id"]}"' in line for line in store.export_rows()), "the export is the ledger"


def test_a_live_approval_cannot_be_deleted():
    for state in ("pending", "executing", "approved"):
        action = _in_state(state)
        with pytest.raises(ValueError, match="still live"):
            store.delete_action(action["id"])
        assert action["id"] in _ids()


def test_an_expired_pending_or_allowance_can_be_deleted():
    for state in ("pending", "approved"):
        action = _in_state(state)
        _expire(action["id"])
        store.delete_action(action["id"])
        assert action["id"] not in _ids()


def test_deleting_twice_is_quiet_and_records_one_event():
    action = _in_state("submitted")
    store.delete_action(action["id"])
    store.delete_action(action["id"])
    assert [e["event"] for e in store.audit(action["id"])].count("deleted") == 1


def test_deleting_something_that_never_existed_is_an_error():
    with pytest.raises(ValueError, match="not found"):
        store.delete_action("nope")


def test_clear_completed_takes_every_finished_one_and_nothing_live():
    finished = [_in_state(s)["id"] for s in ("submitted", "failed", "rejected", "unknown")]
    lapsed = _in_state("pending")
    _expire(lapsed["id"])
    live = [_in_state(s)["id"] for s in ("pending", "executing", "approved")]

    assert store.clear_completed() == 5

    remaining = _ids()
    assert all(i not in remaining for i in finished + [lapsed["id"]])
    assert all(i in remaining for i in live)
    assert store.clear_completed() == 0


def test_the_briefing_and_the_gate_stop_seeing_a_deleted_approval():
    done = _in_state("submitted")
    allowance = _in_state("approved")
    _expire(allowance["id"])
    assert store.briefing()["actions"].get("submitted") == 1
    assert store.find_by_digest(allowance["digest"], "approved") is not None

    store.delete_action(done["id"])
    store.delete_action(allowance["id"])

    assert "submitted" not in store.briefing()["actions"]
    assert store.find_by_digest(allowance["digest"], "approved") is None


def test_an_older_database_gains_the_column_on_init():
    """Installs that predate this have no `deleted` column. `init_db` adds it
    rather than failing every later query."""
    with closing(store.connect()) as conn, conn:
        conn.execute("DROP TABLE business_audit")
        conn.execute("DROP TABLE business_actions")
        conn.execute("""CREATE TABLE business_actions (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
            provider TEXT NOT NULL, operation TEXT NOT NULL, payload TEXT NOT NULL,
            digest TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
            expires REAL NOT NULL, updated REAL NOT NULL, result TEXT NOT NULL DEFAULT '{}')""")
    store.init_db()
    action = _in_state("submitted")
    store.delete_action(action["id"])
    assert action["id"] not in _ids()


# --- the routes -------------------------------------------------------------------

def _client(monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    monkeypatch.setattr(api.web_auth, "origin_allowed", lambda origin: origin == "http://localhost:8340")
    return TestClient(app)


ORIGIN = {"Origin": "http://localhost:8340"}


def test_delete_route_hides_a_finished_one_and_refuses_a_live_one(monkeypatch):
    done = _in_state("submitted")
    live = _in_state("pending")
    with _client(monkeypatch) as client:
        r = client.delete(f"/api/business/actions/{done['id']}", headers=ORIGIN)
        assert r.status_code == 200
        assert r.json()["id"] == done["id"] and isinstance(r.json()["deleted"], float)
        assert client.delete(f"/api/business/actions/{live['id']}", headers=ORIGIN).status_code == 409
        assert client.delete("/api/business/actions/nope", headers=ORIGIN).status_code == 404
        assert client.get("/api/business/actions").json()["items"][0]["id"] == live["id"]


def test_the_brain_cannot_delete_evidence(monkeypatch):
    done = _in_state("submitted")
    with _client(monkeypatch) as client:
        path = f"/api/business/actions/{done['id']}"
        assert client.delete(path).status_code == 403, "no origin: not a browser JARVIS serves"
        assert client.delete(path, headers={"Authorization": "Bearer tool", **ORIGIN}).status_code == 403
        assert client.post("/api/business/actions/clear-completed",
                           headers={"Authorization": "Bearer tool", **ORIGIN}).status_code == 403
    assert done["id"] in _ids()


def test_clear_completed_route_reports_how_many(monkeypatch):
    for state in ("submitted", "failed"):
        _in_state(state)
    live = _in_state("pending")
    with _client(monkeypatch) as client:
        r = client.post("/api/business/actions/clear-completed", headers=ORIGIN)
        assert r.status_code == 200 and r.json() == {"deleted": 2}
        assert [a["id"] for a in client.get("/api/business/actions").json()["items"]] == [live["id"]]
