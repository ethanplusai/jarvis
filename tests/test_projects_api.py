"""The /api/projects/view surface: the dashboard's Projects tab, over HTTP.

The join itself is unit-tested in test_projects_view.py; this file exercises
the three endpoints (list, detail, open) and the two things only the server
layer can get wrong: wiring `_snapshot_or_empty()` (not the raw roster, so
JARVIS's own runs stay uncounted) and validating `open`'s path against the
project's own known directories before handing it to `actions`.
"""

import importlib
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import session_watch as sw

# What the dashboard's own page sends. `POST /api/projects/open` opens an
# editor, a Terminal or a browser on this machine, so the app refuses it
# from anywhere that is not a page JARVIS serves.
BROWSER = {"Origin": "http://localhost:5173"}


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
    return server_module, run_store


class _Watcher:
    def __init__(self, sessions):
        self.snapshot = sw.Snapshot(sessions=sessions, taken_at=1.0)


def _session(session_id, project, cwd, state=sw.IDLE):
    return sw.SessionState(
        session_id=session_id, cwd=cwd, project=project, state=state,
        roster_name=project, voice_name=project, steerable=True, since=10.0)


class _Actions:
    def __init__(self, success=True):
        self.editor: list[str] = []
        self.terminal: list[tuple[str, str]] = []   # (directory, command)
        self.browser: list[str] = []
        self.success = success

    async def open_in_editor(self, path):
        self.editor.append(path)
        return {"success": self.success, "editor": "VS Code",
                "confirmation": "Opened, sir."}

    async def open_terminal_at(self, path, command=""):
        self.terminal.append((path, command))
        return {"success": self.success, "confirmation": "Terminal's open, sir."}

    async def open_browser(self, url, browser="chrome"):
        self.browser.append(url)
        return {"success": self.success, "confirmation": "Opened, sir."}


# --- list --------------------------------------------------------------------

def test_empty_roster_and_no_runs_is_an_empty_list(wired):
    server, _store = wired
    with TestClient(server.app, headers=BROWSER) as c:
        r = c.get("/api/projects/view")
    assert r.status_code == 200
    assert r.json()["projects"] == []


def test_a_project_from_the_roster_appears(wired):
    server, _store = wired
    with TestClient(server.app, headers=BROWSER) as c:
        server.session_watcher = _Watcher([_session("s1", "chitauri", "/p/chitauri")])
        r = c.get("/api/projects/view")
    projects = r.json()["projects"]
    assert [p["name"] for p in projects] == ["chitauri"]
    assert projects[0]["primary_path"] == "/p/chitauri"
    # The list shape is the CHEAP one: no session/run payload.
    assert "sessions" not in projects[0]
    assert "runs" not in projects[0]


def test_jarvis_own_run_session_is_not_counted_as_a_conversation(wired):
    """The exact bug this project's CLAUDE.md names: 12 conversations became
    16 when JARVIS's own spawned runs were counted as the user's work."""
    server, store = wired
    run_id = store.create_run("build it", "tony-starks-website", "/tmp/tsw", "voice")
    with TestClient(server.app, headers=BROWSER) as c:
        server.session_watcher = _Watcher([
            _session("a-real-conversation", "tony-starks-website", "/tmp/tsw"),
            _session(run_id, "tony-starks-website", "/tmp/tsw"),
        ])
        server._run_ids_cache = (0.0, frozenset())
        r = c.get("/api/projects/view")
    projects = r.json()["projects"]
    assert projects[0]["session_count"] == 1


def test_a_project_known_only_from_a_run_still_appears(wired):
    server, store = wired
    store.create_run("build it", "hammer", "/tmp/hammer", "voice")
    with TestClient(server.app, headers=BROWSER) as c:
        r = c.get("/api/projects/view")
    projects = r.json()["projects"]
    assert [p["name"] for p in projects] == ["hammer"]
    assert projects[0]["latest_run"]["project_name"] == "hammer"


# --- detail --------------------------------------------------------------------

def test_detail_404_for_an_unknown_project(wired):
    server, _store = wired
    with TestClient(server.app, headers=BROWSER) as c:
        r = c.get("/api/projects/view/nonexistent")
    assert r.status_code == 404


def test_detail_reports_a_missing_directory_plainly(wired, tmp_path):
    server, _store = wired
    with TestClient(server.app, headers=BROWSER) as c:
        server.session_watcher = _Watcher(
            [_session("s1", "ghost", str(tmp_path / "gone"))])
        r = c.get("/api/projects/view/ghost")
    body = r.json()["project"]
    assert body["directory_exists"] is False
    assert body["repo"]["exists"] is False


def test_detail_returns_repo_and_build_summaries(wired, tmp_path):
    server, _store = wired
    project = tmp_path / "demo"
    project.mkdir()
    (project / "README.md").write_text("# demo\n\nA small tool.\n")
    (project / "main.py").write_text("print(1)\n")
    spec_dir = project / "docs" / "superpowers" / "specs"
    spec_dir.mkdir(parents=True)
    (spec_dir / "2026-01-01-thing-design.md").write_text("# Thing\n")

    with TestClient(server.app, headers=BROWSER) as c:
        server.session_watcher = _Watcher([_session("s1", "demo", str(project))])
        r = c.get("/api/projects/view/demo")
    body = r.json()["project"]
    assert body["directory_exists"] is True
    assert body["repo"]["exists"] is True
    assert "Python" in body["repo"]["headline"]
    assert body["build"]["has_spec"] is True
    assert body["build"]["has_plan"] is False
    assert body["sessions"][0]["session_id"] == "s1"


# --- open --------------------------------------------------------------------

def test_open_rejects_a_path_not_belonging_to_the_project(wired, monkeypatch):
    server, _store = wired
    fake = _Actions()
    with TestClient(server.app, headers=BROWSER) as c:
        monkeypatch.setattr(server, "actions", fake)
        server.session_watcher = _Watcher([_session("s1", "chitauri", "/p/chitauri")])
        r = c.post("/api/projects/open",
                   json={"name": "chitauri", "path": "/etc/passwd", "target": "editor"})
    assert r.status_code == 400
    assert fake.editor == []


def test_open_rejects_an_unknown_project(wired, monkeypatch):
    server, _store = wired
    fake = _Actions()
    with TestClient(server.app, headers=BROWSER) as c:
        monkeypatch.setattr(server, "actions", fake)
        r = c.post("/api/projects/open",
                   json={"name": "nope", "path": "/p", "target": "editor"})
    assert r.status_code == 400
    assert fake.editor == []


def test_open_in_editor_calls_actions(wired, monkeypatch):
    server, _store = wired
    fake = _Actions()
    with TestClient(server.app, headers=BROWSER) as c:
        monkeypatch.setattr(server, "actions", fake)
        server.session_watcher = _Watcher([_session("s1", "chitauri", "/p/chitauri")])
        r = c.post("/api/projects/open",
                   json={"name": "chitauri", "path": "/p/chitauri", "target": "editor"})
    assert r.status_code == 200
    assert r.json()["success"] is True
    assert fake.editor == ["/p/chitauri"]


def test_open_in_terminal_calls_actions(wired, monkeypatch):
    server, _store = wired
    fake = _Actions()
    with TestClient(server.app, headers=BROWSER) as c:
        monkeypatch.setattr(server, "actions", fake)
        server.session_watcher = _Watcher([_session("s1", "chitauri", "/p/chitauri")])
        r = c.post("/api/projects/open",
                   json={"name": "chitauri", "path": "/p/chitauri", "target": "terminal"})
    assert r.status_code == 200
    assert fake.terminal == [("/p/chitauri", "")]


def test_open_in_browser_calls_actions_with_a_file_uri(wired, monkeypatch):
    server, _store = wired
    fake = _Actions()
    # `/p/chitauri` on macOS; `C:\p\chitauri` on Windows, where a path with
    # no drive is not absolute and has no file URI.
    where = os.path.abspath("/p/chitauri")
    with TestClient(server.app, headers=BROWSER) as c:
        monkeypatch.setattr(server, "actions", fake)
        server.session_watcher = _Watcher([_session("s1", "chitauri", where)])
        r = c.post("/api/projects/open",
                   json={"name": "chitauri", "path": where, "target": "browser"})
    assert r.status_code == 200
    assert fake.browser == [Path(where).as_uri()]


def test_open_reports_actions_failure(wired, monkeypatch):
    server, _store = wired
    fake = _Actions(success=False)
    with TestClient(server.app, headers=BROWSER) as c:
        monkeypatch.setattr(server, "actions", fake)
        server.session_watcher = _Watcher([_session("s1", "chitauri", "/p/chitauri")])
        r = c.post("/api/projects/open",
                   json={"name": "chitauri", "path": "/p/chitauri", "target": "editor"})
    assert r.status_code == 200
    assert r.json()["success"] is False
