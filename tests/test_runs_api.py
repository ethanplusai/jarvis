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
    # The dashboard's own Origin: creating, cancelling and retrying a run
    # are refused from anywhere else. test_web_security.py is where that is
    # proved; here it is just how the page talks.
    with TestClient(server.app,
                    headers={"Origin": "http://localhost:5173"}) as c:
        yield c, run_store


def test_list_runs_empty(client):
    c, _ = client
    r = c.get("/api/runs")
    assert r.status_code == 200
    assert r.json()["runs"] == []


def test_list_runs_returns_created(client):
    c, store = client
    store.create_run("build", "proj", "/tmp/proj", "voice")
    runs = c.get("/api/runs").json()["runs"]
    assert len(runs) == 1
    assert runs[0]["prompt"] == "build"


def test_list_runs_status_filter(client):
    c, store = client
    a = store.create_run("a", "p", "/tmp/p", "voice")
    store.create_run("b", "p", "/tmp/p", "voice")
    store.update_run(a, status=store.RunStatus.SUCCEEDED)
    runs = c.get("/api/runs?status=succeeded").json()["runs"]
    assert [r["id"] for r in runs] == [a]


def test_get_run(client):
    c, store = client
    run_id = store.create_run("build", "proj", "/tmp/proj", "voice")
    r = c.get(f"/api/runs/{run_id}")
    assert r.status_code == 200
    assert r.json()["run"]["id"] == run_id


def test_get_run_404(client):
    c, _ = client
    assert c.get("/api/runs/missing").status_code == 404


def test_get_events(client):
    c, store = client
    run_id = store.create_run("build", "proj", "/tmp/proj", "voice")
    store.append_event(run_id, 1, "assistant", '{"a":1}')
    store.append_event(run_id, 2, "result", '{"b":2}')
    events = c.get(f"/api/runs/{run_id}/events").json()["events"]
    assert [e["seq"] for e in events] == [1, 2]


def test_get_events_after_seq(client):
    c, store = client
    run_id = store.create_run("build", "proj", "/tmp/proj", "voice")
    store.append_event(run_id, 1, "assistant", "{}")
    store.append_event(run_id, 2, "result", "{}")
    events = c.get(f"/api/runs/{run_id}/events?after_seq=1").json()["events"]
    assert [e["seq"] for e in events] == [2]


def test_get_events_reports_total(client):
    c, store = client
    run_id = store.create_run("build", "proj", "/tmp/proj", "voice")
    for i in range(1, 6):
        store.append_event(run_id, i, "assistant", "{}")
    body = c.get(f"/api/runs/{run_id}/events?after_seq=3&limit=10").json()
    assert body["total"] == 5
    assert [e["seq"] for e in body["events"]] == [4, 5]


def test_cancel_unknown_returns_404(client):
    c, _ = client
    assert c.delete("/api/runs/missing").status_code == 404


def test_retry_unknown_returns_404(client):
    c, _ = client
    assert c.post("/api/runs/missing/retry").status_code == 404


@pytest.mark.parametrize("status", ["queued", "running"])
def test_retry_active_run_is_409(client, status):
    """Retrying a run that has not finished would double-spawn it: two
    processes in the same directory, both forked from the same session."""
    c, store = client
    run_id = store.create_run("build", "proj", "/tmp/proj", "voice")
    store.update_run(run_id, status=status)

    r = c.post(f"/api/runs/{run_id}/retry")
    assert r.status_code == 409
    assert len(store.list_runs(limit=10)) == 1, "no second run may be created"


@pytest.mark.parametrize("status", ["succeeded", "failed", "timed_out",
                                    "cancelled"])
def test_retry_terminal_run_spawns(client, status, monkeypatch):
    c, store = client
    import server

    spawned = []

    async def fake_spawn(prompt, name, path, origin, resume_from=None,
                         timeout_sec=0):
        spawned.append((prompt, name, path, origin, resume_from))
        return store.create_run(prompt, name, path, origin, resume_from)

    monkeypatch.setattr(server.run_executor_instance, "spawn", fake_spawn)
    run_id = store.create_run("build", "proj", "/tmp/proj", "voice")
    store.update_run(run_id, status=status, started_at=1)

    r = c.post(f"/api/runs/{run_id}/retry")
    assert r.status_code == 200
    assert spawned[0][4] == run_id, "the retry must fork from the original"


@pytest.mark.parametrize("started", [False, True])
def test_retry_preserves_configuration_and_only_resumes_started_runs(client, monkeypatch, started):
    c, store = client
    import server
    captured = {}
    async def spawn(*args, **kwargs):
        captured.update(kwargs)
        return "new-run"
    monkeypatch.setattr(server.run_executor_instance, "spawn", spawn)
    run_id = store.create_run("p", "p", "/tmp/p", "api")
    store.update_run(run_id, status="failed", requested_model="opus", timeout_sec=123,
                     started_at=1 if started else None)
    assert c.post(f"/api/runs/{run_id}/retry").status_code == 200
    assert captured == {"model": "opus", "timeout_sec": 123,
                        "resume_from": run_id if started else None}


def test_stats_shape(client):
    c, store = client
    run_id = store.create_run("a", "p", "/tmp/p", "voice")
    store.update_run(run_id, status=store.RunStatus.SUCCEEDED, cost_usd=0.5)
    body = c.get("/api/runs/stats?period=day").json()
    assert body["total_runs"] == 1
    assert body["total_cost_usd"] == 0.5
    assert body["by_status"]["succeeded"] == 1


def test_tasks_alias_is_gone(client):
    """The deprecated unguarded /api/tasks alias was removed."""
    c, store = client
    store.create_run("legacy", "p", "/tmp/p", "api")
    assert c.get("/api/tasks").status_code == 404


# -- FINDING 1: limit / after_seq must be clamped on both ends -------------


def test_list_runs_negative_limit_is_clamped(client):
    c, store = client
    # Seed more rows than the clamp (200) would allow, so a test that
    # passes only because the table is small proves nothing.
    for i in range(210):
        store.create_run(f"p{i}", "proj", "/tmp/proj", "voice")
    runs = c.get("/api/runs?limit=-1").json()["runs"]
    assert 0 < len(runs) <= 200


def test_list_runs_zero_limit_is_clamped_to_at_least_one(client):
    c, store = client
    for i in range(5):
        store.create_run(f"p{i}", "proj", "/tmp/proj", "voice")
    runs = c.get("/api/runs?limit=0").json()["runs"]
    assert len(runs) == 1


def test_get_events_negative_limit_is_clamped(client):
    c, store = client
    run_id = store.create_run("build", "proj", "/tmp/proj", "voice")
    for i in range(1, 510):
        store.append_event(run_id, i, "assistant", "{}")
    events = c.get(f"/api/runs/{run_id}/events?limit=-1").json()["events"]
    assert 0 < len(events) <= 500


def test_get_events_negative_after_seq_behaves_like_zero(client):
    c, store = client
    run_id = store.create_run("build", "proj", "/tmp/proj", "voice")
    # Seed a couple of non-positive seqs directly (next_seq() never hands
    # these out itself) so a clamp to 0 is actually observable: unclamped,
    # `seq > -5` would include them; clamped to `seq > 0`, it must not.
    store.append_event(run_id, -2, "assistant", "{}")
    store.append_event(run_id, 0, "assistant", "{}")
    store.append_event(run_id, 1, "assistant", "{}")
    store.append_event(run_id, 2, "result", "{}")
    r = c.get(f"/api/runs/{run_id}/events?after_seq=-5")
    assert r.status_code == 200
    assert [e["seq"] for e in r.json()["events"]] == [1, 2]


# -- FINDING 2: empty project_path must be rejected, not defaulted ---------


def test_create_run_missing_project_path_returns_400(client):
    c, store = client
    r = c.post("/api/runs", json={"prompt": "do something"})
    assert r.status_code == 400
    assert c.get("/api/runs").json()["runs"] == []


def test_create_run_blank_project_path_returns_400(client):
    c, store = client
    r = c.post("/api/runs", json={"prompt": "do something",
                                  "project_path": "   "})
    assert r.status_code == 400
    assert c.get("/api/runs").json()["runs"] == []


def test_create_run_with_valid_project_path_succeeds(client, monkeypatch):
    c, store = client
    import server

    spawned = {}

    async def fake_spawn(prompt, project_name, project_path, origin,
                         resume_from=None, timeout_sec=0):
        spawned["args"] = (prompt, project_name, project_path, origin)
        return "fake-run-id"

    monkeypatch.setattr(server.run_executor_instance, "spawn", fake_spawn)

    r = c.post("/api/runs", json={"prompt": "do something",
                                  "project_path": "/tmp/some/project"})
    assert r.status_code == 200
    assert r.json()["run_id"] == "fake-run-id"
    assert spawned["args"][2] == "/tmp/some/project"
