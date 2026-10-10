"""The brain's own tools fill the project map; a browser is not required.

Measured live, 2026-09-22. JARVIS told the user his own repository was not
registered. It was: `read_file` returned the file from it in the same
minute. But `cached_projects` starts EMPTY at every startup
(server.py:1814) and the only two places that ever fill it are HTTP request
handlers — `/api/projects` (server.py:6680) and `/api/specs`. Nothing on
the startup path, no background task, no timer.

So until a browser happened to ask, the map the resolver and the listing
both read had nothing in it, and their only other source is the session
watcher, which sees a project only while a Claude Code conversation is open
in it. Three of thirteen qualified. The other ten did not exist as far as
the brain was concerned, and he said so with confidence.

Making the listing agree with the resolver did not help: they were then
consistently blind. The map has to be filled by whoever needs it, and the
brain reaches every one of his tools through one door.

The cost lands on the first tool call after a restart rather than on every
boot: the scan walks every configured root with a 20s budget, and one of
those roots can be a cloud-backed Desktop.
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
    import server as server_module
    importlib.reload(server_module)
    run_store.init_db()
    return server_module


class _NoSessions:
    def by_project(self):
        return {}


def _client(server):
    """One server lifetime. Each TestClient context runs `lifespan`, which
    resets the map on purpose — so several calls that are meant to share a
    warm map must share one client, as they do against a real server."""
    from fastapi.testclient import TestClient
    return TestClient(server.app)


def _post(client, tool, arguments=None):
    import data_paths
    token = data_paths.ensure_tool_token()
    return client.post("/internal/tool",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"tool": tool, "arguments": arguments or {}})


def _call(server, tool, arguments=None):
    with _client(server) as client:
        return _post(client, tool, arguments)


def _scan_returns(server, monkeypatch, entries, counter):
    async def fake_scan():
        counter.append(1)
        return [{"name": n, "path": p} for n, p in entries]
    monkeypatch.setattr(server, "scan_projects", fake_scan)
    monkeypatch.setattr(server, "_snapshot_or_empty", lambda: _NoSessions())
    monkeypatch.setattr(server, "cached_projects", [])


def test_the_first_tool_call_fills_an_empty_map(wired, monkeypatch):
    """The reported failure: no browser has been near this server."""
    server = wired
    calls = []
    _scan_returns(server, monkeypatch,
                  [("stark-armory-next", "/dev/workshop/stark-armory-next")], calls)

    body = _call(server, "list_projects").json()

    assert calls, "no scan was run: the map is still empty and the brain is blind"
    assert "stark-armory-next" in body["text"], body["text"]


def test_the_resolver_sees_it_too_without_a_browser(wired, monkeypatch):
    """`read_file` and the other eleven handlers resolve through the same
    map, so warming it at the door covers all of them."""
    server = wired
    calls = []
    _scan_returns(server, monkeypatch, [("hammer", "/p/hammer")], calls)

    _call(server, "list_projects")
    name, path, problem = server._resolve_project_or_explain("hammer")
    assert (name, path, problem) == ("hammer", "/p/hammer", None)


def test_a_warm_map_is_not_rescanned_on_every_tool_call(wired, monkeypatch):
    """The scan walks every root with a 20s budget. Paying that per tool
    call would be worse than the bug."""
    server = wired
    calls = []
    _scan_returns(server, monkeypatch, [("hammer", "/p/hammer")], calls)

    with _client(server) as client:
        for _ in range(4):
            _post(client, "list_projects")
    assert len(calls) == 1, f"scanned {len(calls)} times; the map was already full"


def test_a_machine_with_no_projects_is_not_rescanned_forever(wired, monkeypatch):
    """An empty result is a RESULT. Retrying it on every tool call would put
    a filesystem walk in front of every single thing the brain does."""
    server = wired
    calls = []
    _scan_returns(server, monkeypatch, [], calls)

    with _client(server) as client:
        for _ in range(4):
            _post(client, "usage_status")
    assert len(calls) == 1, f"scanned {len(calls)} times over an empty result"


def test_a_failing_scan_does_not_take_the_tool_down_with_it(wired, monkeypatch):
    """A slow or broken root must cost the brain its project list, not its
    ability to answer at all."""
    server = wired

    async def boom():
        raise OSError("the Desktop root is a cloud drive and it is offline")
    monkeypatch.setattr(server, "scan_projects", boom)
    monkeypatch.setattr(server, "_snapshot_or_empty", lambda: _NoSessions())
    monkeypatch.setattr(server, "cached_projects", [])

    r = _call(server, "usage_status")
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True, r.json()


def test_a_scan_that_found_nothing_because_it_broke_is_tried_again(wired, monkeypatch):
    """The flag was set BEFORE the scan. On a cloud-backed Desktop the first
    walk hits its 20s budget and returns nothing, and that emptiness then
    stood for the rest of the process: every later tool call skipped the
    scan and the brain saw only projects with a session open. The permanent
    version of the bug this whole file exists for."""
    server = wired
    calls = []

    async def fails_then_works():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("the Desktop root is a cloud drive and it is offline")
        return [{"name": "hammer", "path": "/p/hammer"}]
    monkeypatch.setattr(server, "scan_projects", fails_then_works)
    monkeypatch.setattr(server, "_snapshot_or_empty", lambda: _NoSessions())
    monkeypatch.setattr(server, "cached_projects", [])

    with _client(server) as client:
        assert _post(client, "usage_status").status_code == 200
        body = _post(client, "list_projects").json()
    assert len(calls) == 2, "a failed scan latched and was never retried"
    assert "hammer" in body["text"], body["text"]


def test_a_scan_that_legitimately_found_nothing_is_not_retried(wired, monkeypatch):
    """Still only once when the answer really is 'no projects'."""
    server = wired
    calls = []
    _scan_returns(server, monkeypatch, [], calls)
    with _client(server) as client:
        for _ in range(4):
            _post(client, "usage_status")
    assert len(calls) == 1, f"scanned {len(calls)} times over an honest empty result"
