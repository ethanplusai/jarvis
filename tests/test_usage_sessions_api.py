"""/api/usage/sessions — per-session token usage, served.

The endpoint's job beyond serving `usage_scan.snapshot()`:

  * point the scan at the SAME roots session_watch reads, so the Usage tab
    and the Sessions tab are describing the same machine;
  * hand it the set of run ids, so JARVIS's own one-shot runs land in their
    own bucket instead of swelling the user's totals;
  * never repeat a 3-second cold scan on every poll;
  * fail to an honest "nothing measured", never to a confident zero.

The scan also runs off the event loop (`asyncio.to_thread`). That is NOT
asserted here and should not be claimed to be: TestClient drives the app on
its own thread, so a synchronous route body passes every test in this file.
The deadline test below proves the route answers, not where it ran.
"""

import importlib
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.fixtures.transcripts import write_agent_transcript, write_transcript

# Read the clock when a test RUNS, never at import. This file once captured
# time.time() at module level, and the "active agent" test wrote its turn
# five seconds before IMPORT, not five seconds before the scan. In a full
# suite run the gap between import and this test drifts past the 90-second
# activity window and the agent reads as idle: green alone, red under load,
# and worse every time the suite grows. It flaked twice in one night before
# anyone looked.
def _now() -> float:
    return time.time()


HOUR = 3600.0


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A server whose transcript roots are a tmp dir, not the real machine."""
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    roots = [tmp_path / ".claude", tmp_path / ".claude-orcha"]
    for r in roots:
        (r / "projects").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JARVIS_CLAUDE_CONFIG_DIRS", os.pathsep.join(str(r) for r in roots))

    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import session_watch
    importlib.reload(session_watch)
    import usage_scan
    importlib.reload(usage_scan)
    # The scan's cache and lock live in usage_api (the router carved out of
    # server.py); reload it too, or every test shares the first one's scan.
    import usage_api
    importlib.reload(usage_api)
    import server
    importlib.reload(server)
    run_store.init_db()
    # config_roots() always includes the two real defaults; point the scan at
    # the fixture roots only, or the test reads the developer's own machine.
    monkeypatch.setattr(server.session_watch, "DEFAULT_ROOTS", ())
    return roots, server, run_store


@pytest.fixture
def client(env):
    roots, server, run_store = env
    with TestClient(server.app) as c:
        yield c, roots, run_store


def test_an_empty_machine_reports_no_measurement_not_zero(client):
    c, _roots, _rs = client

    body = c.get("/api/usage/sessions").json()

    assert body["measured"] is False
    assert body["sessions"] == []
    assert body["daily"] == []


def test_a_conversations_tokens_and_its_subagents_are_both_served(client):
    c, roots, _rs = client
    a = roots[0]
    write_transcript(a, cwd="/p/one", session_id="s1",
                     turns=[dict(when=_now() - HOUR, inp=10, out=20,
                                 cache_read=30, cache_creation=40)])
    write_agent_transcript(a, cwd="/p/one", session_id="s1", agent_id="ag1",
                           turns=[dict(when=_now() - 5, out=7)])

    body = c.get("/api/usage/sessions").json()

    assert body["measured"] is True
    s, = body["sessions"]
    assert s["session_id"] == "s1" and s["project"] == "one"
    assert s["tokens"]["output"] == 20
    assert s["tokens"]["total"] == 100
    assert s["agent_tokens"]["output"] == 7
    assert s["agents"][0]["agent_id"] == "ag1"
    assert s["agents"][0]["active"] is True
    assert body["active_agents"] == 1


def test_a_run_jarvis_spawned_is_not_counted_as_the_users_work(client):
    """A run id IS a Claude Code session id, so a run's transcript is
    indistinguishable from a conversation until the run table is consulted.
    Live, two runs turned "12 conversations" into "16" once already."""
    c, roots, run_store = client
    run_id = run_store.create_run("build it", "one", "/p/one", "voice")
    write_transcript(roots[0], cwd="/p/one", session_id="mine",
                     turns=[dict(when=_now() - HOUR, out=11)])
    write_transcript(roots[0], cwd="/p/one", session_id=run_id,
                     turns=[dict(when=_now() - HOUR, out=999)])

    body = c.get("/api/usage/sessions").json()

    assert [s["session_id"] for s in body["sessions"]] == ["mine"]
    assert body["totals"]["output"] == 11
    assert body["own_totals"]["output"] == 999
    assert [s["session_id"] for s in body["own_sessions"]] == [run_id]


def test_the_scan_is_not_repeated_on_every_request(client):
    """A cold scan is 548 MB and ~3 s. Polling it once a minute per open tab
    must not mean re-walking the disk each time."""
    c, roots, _rs = client
    write_transcript(roots[0], cwd="/p/one", session_id="s1",
                     turns=[dict(when=_now() - HOUR, out=10)])

    first = c.get("/api/usage/sessions").json()
    second = c.get("/api/usage/sessions").json()

    assert first["scanned_at"] == second["scanned_at"], "the second read re-scanned"
    assert second["sessions"][0]["tokens"]["output"] == 10


def test_a_scan_that_blows_up_says_so_rather_than_serving_zeroes(client, monkeypatch):
    c, _roots, _rs = client
    import server

    def boom(*a, **k):
        raise OSError("the disk went away")

    import usage_api
    monkeypatch.setattr(usage_api.usage_scan, "snapshot", boom)
    usage_api._usage_scan_result = (0.0, {})

    res = c.get("/api/usage/sessions")

    assert res.status_code == 503
    body = res.json()
    assert body["measured"] is False
    assert body["error"]


def test_the_endpoint_answers_within_a_deadline(client):
    """A request that hangs looks exactly like a slow machine, and a test
    that waits for it hangs the suite instead of failing it — which has
    already happened here once.

    So the request is made on a worker thread and the DEADLINE is the
    assertion. `TestClient.get` is synchronous: calling it inline and timing
    it afterwards would never reach the assertion at all.
    """
    c, roots, _rs = client
    write_transcript(roots[0], cwd="/p/one", session_id="s1",
                     turns=[dict(when=_now() - HOUR, out=10)])

    answer: list = []
    worker = threading.Thread(
        target=lambda: answer.append(c.get("/api/usage/sessions")),
        daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(timeout=20)
    elapsed = time.monotonic() - started

    assert not worker.is_alive(), f"the endpoint had not answered after {elapsed:.0f}s"
    assert answer[0].status_code == 200
    assert answer[0].json()["sessions"][0]["tokens"]["output"] == 10


def test_the_scan_reads_the_same_roots_the_session_watcher_does(client):
    """Two views of one machine. If the Usage tab read a different set of
    roots from the Sessions tab, a conversation could appear in one and not
    the other and nobody would know which was wrong."""
    c, roots, _rs = client
    import session_watch
    write_transcript(roots[1], cwd="/p/two", session_id="s2",
                     turns=[dict(when=_now() - HOUR, out=5)])

    body = c.get("/api/usage/sessions").json()

    assert set(body["roots"]) == {str(r) for r in session_watch.config_roots()}
    assert [s["session_id"] for s in body["sessions"]] == ["s2"]
