"""The review surface, as the page and as JARVIS see it.

`/api/specs` and `/api/specs/doc` feed the SPECS tab; `review_document` and
`approve_document` are how the same documents reach the conversation. The
property this file exists to protect is the one the whole surface rests on:

    the number the user says means the same section on the page and in
    JARVIS's mouth.

It is guaranteed structurally — both sides call `specs.read_document`, and
`test_the_page_and_jarvis_are_handed_the_same_numbering` fails if either ever
starts counting for itself.

Nothing here spawns a process, opens a socket to the outside, or writes
outside tmp_path.
"""

import importlib
import os
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

import builds  # noqa: E402
import specs  # noqa: E402

SPEC_BODY = """\
A local web UI to browse and edit CLAUDE.md files

A small local page that lists every CLAUDE.md on this machine.
"""

PLAN = """\
# CLAUDE.md browser — Implementation Plan

## Task 1: Read the files off disk

- [x] Walk the home directory
- [x] Cap the walk

## Task 2: Serve the list

- [ ] A JSON endpoint
- [ ] Sort by modification time
"""


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """A reloaded server with exactly one project, on disk, in tmp_path."""
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server
    importlib.reload(server)
    run_store.init_db()

    root = tmp_path / "claude-browser"
    root.mkdir()
    relative = builds.write_spec(str(root), SPEC_BODY)
    plan = root / builds.PLAN_DIR / "2026-09-03-plan.md"
    plan.write_text(PLAN, encoding="utf-8")
    # The plan is the newest document, by the clock and not by luck: on
    # Windows two files written this close together share an mtime.
    spec_mtime = (root / relative).stat().st_mtime
    os.utime(plan, (spec_mtime + 1, spec_mtime + 1))

    server.cached_projects = [
        {"name": "claude-browser", "path": str(root), "branch": "main"}]

    async def _no_rescan():
        return server.cached_projects

    monkeypatch.setattr(server, "scan_projects", _no_rescan)
    return server, root, relative


@pytest.fixture
def client(wired):
    """The same server, with its lifespan run.

    `lifespan` clears `cached_projects` on startup, so the project is put
    back after TestClient has entered rather than before.
    """
    server, root, relative = wired
    entry = list(server.cached_projects)
    # The dashboard's own Origin: /ws/specs refuses a handshake from a page
    # JARVIS does not serve. See test_web_security.py.
    with TestClient(server.app,
                    headers={"Origin": "http://localhost:5173"}) as c:
        server.cached_projects = entry
        yield c, server, root, relative


# ---------------------------------------------------------------------------
# GET /api/specs — the master list
# ---------------------------------------------------------------------------

def test_the_list_carries_every_project_with_something_to_review(client):
    c, _, root, _ = client
    body = c.get("/api/specs").json()
    assert [p["name"] for p in body["projects"]] == ["claude-browser"]
    project = body["projects"][0]
    assert project["path"] == str(root)
    assert {d["kind"] for d in project["documents"]} == {"spec", "plan"}


def test_a_project_with_no_documents_is_not_listed(client, tmp_path):
    c, server, _, _ = client
    bare = tmp_path / "bare"
    bare.mkdir()
    server.cached_projects.append(
        {"name": "bare", "path": str(bare), "branch": ""})
    assert [p["name"] for p in c.get("/api/specs").json()["projects"]] == [
        "claude-browser"]


# ---------------------------------------------------------------------------
# A project with a git worktree
#
# `session_watch.project_name` collapses `<repo>/.claude/worktrees/<branch>`
# to the repo name, deliberately — two worktrees of one repo ARE one project.
# `_specs_projects` then saw two paths under one name and dropped it, so a
# project with any Claude Code worktree rendered as "Nothing to review yet"
# while its specs sat on disk.
#
# Refusing to BUILD on an ambiguous name stays right (`start_build` still
# refuses). Refusing to SHOW what exists does not: the tab lists every path,
# labelled.
# ---------------------------------------------------------------------------

def _worktree_of(root: Path, branch: str) -> Path:
    """The layout Claude Code creates: <repo>/.claude/worktrees/<branch>."""
    tree = root / ".claude" / "worktrees" / branch
    tree.mkdir(parents=True)
    return tree


@pytest.fixture
def with_worktree(client):
    """The one project, plus a worktree of it carrying its own spec."""
    c, server, root, relative = client
    tree = _worktree_of(root, "runs-dashboard")
    builds.write_spec(str(tree), "A different design, in the worktree\n\nx\n")
    server.cached_projects.append(
        {"name": "claude-browser", "path": str(tree), "branch": "runs-dashboard"})
    return c, server, root, tree


def test_a_project_with_a_worktree_still_lists_its_documents(with_worktree):
    """The reproduction: two paths under one name is not "nothing exists"."""
    c, _, root, tree = with_worktree

    projects = c.get("/api/specs").json()["projects"]

    assert projects, "a project with a worktree rendered as nothing to review"
    assert {p["path"] for p in projects} == {str(root), str(tree)}
    assert {p["name"] for p in projects} == {"claude-browser"}


def test_each_copy_says_where_it_is(with_worktree):
    """Two rows with one name and no way to tell them apart would be worse
    than the bug. The worktree says which worktree it is."""
    c, _, root, tree = with_worktree

    by_path = {p["path"]: p for p in c.get("/api/specs").json()["projects"]}

    assert by_path[str(tree)]["where"] == "worktree runs-dashboard"
    assert by_path[str(root)]["where"], "the main checkout needs a label too"
    assert by_path[str(tree)]["where"] != by_path[str(root)]["where"]


def test_an_unambiguous_project_carries_no_label(client):
    c, _, _, _ = client
    project, = c.get("/api/specs").json()["projects"]
    assert project["where"] == ""


def test_a_document_is_fetched_from_the_copy_it_was_listed_under(with_worktree):
    """The list and the reader have to agree, or the page shows one
    project's spec under another's name."""
    c, _, root, tree = with_worktree
    listed = {p["path"]: p for p in c.get("/api/specs").json()["projects"]}
    doc = listed[str(tree)]["documents"][0]

    body = c.get("/api/specs/doc", params={"project": "claude-browser",
                                           "root": str(tree),
                                           "path": doc["path"]}).json()

    assert "worktree" in body["preamble"] + "".join(
        s["body"] for s in body["sections"])


def test_a_root_that_is_not_one_of_the_projects_own_is_a_404(with_worktree):
    """`root` arrives from a URL like everything else here and is checked
    against the project's own known directories, never trusted."""
    c, _, root, tree = with_worktree
    for probe in ("/etc", str(root.parent), f"{root}/../..", ""):
        r = c.get("/api/specs/doc", params={"project": "claude-browser",
                                            "root": probe,
                                            "path": "docs/superpowers"})
        assert r.status_code == 404, probe


def test_the_socket_notices_a_change_in_either_copy(monkeypatch, with_worktree):
    """The fingerprint has to carry the path, or a change in the worktree
    looks identical to no change at all."""
    c, _, _, tree = with_worktree
    monkeypatch.setenv("JARVIS_SPECS_POLL", "0.05")
    with c.websocket_connect("/ws/specs") as ws:
        assert _next_message(ws)["type"] == "hello"
        doc = next((tree / builds.SPEC_DIR).glob("*.md"))
        doc.write_text(doc.read_text(encoding="utf-8") + "\n## Added\n\nx\n",
                       encoding="utf-8")
        assert _next_message(ws)["type"] == "changed"


def test_an_unapproved_document_makes_the_project_await_the_user(client):
    c, _, _, _ = client
    assert c.get("/api/specs").json()["projects"][0]["state"] == "awaiting"


def test_once_approved_a_plan_in_flight_reads_as_building(client):
    c, _, root, _ = client
    for d in specs.list_documents(str(root)):
        specs.record_approval(str(root), d["path"])
    project = c.get("/api/specs").json()["projects"][0]
    assert project["state"] == "building"
    assert project["progress"]["done"] == 1
    assert project["progress"]["total"] == 2


def test_a_finished_plan_reads_as_work_awaiting_review(client):
    c, _, root, _ = client
    plan = root / builds.PLAN_DIR / "2026-09-03-plan.md"
    plan.write_text(plan.read_text(encoding="utf-8").replace("- [ ]", "- [x]"),
                    encoding="utf-8")
    for d in specs.list_documents(str(root)):
        specs.record_approval(str(root), d["path"])
    assert c.get("/api/specs").json()["projects"][0]["state"] == "review"


# ---------------------------------------------------------------------------
# GET /api/specs/doc — the document itself
# ---------------------------------------------------------------------------

def test_the_document_arrives_numbered(client):
    c, _, _, relative = client
    body = c.get("/api/specs/doc",
                 params={"project": "claude-browser", "path": relative}).json()
    assert body["kind"] == "spec"
    assert body["approval"]["state"] == "awaiting"
    assert [s["number"] for s in body["sections"]] == [1]


def test_a_plan_arrives_with_its_task_progress(client):
    c, _, _, _ = client
    body = c.get("/api/specs/doc",
                 params={"project": "claude-browser",
                         "path": f"{builds.PLAN_DIR}/2026-09-03-plan.md"}).json()
    assert body["progress"]["done"] == 1
    assert body["progress"]["current_section"] == 2


def test_a_traversal_is_a_404_like_any_other_miss(client):
    """Never a 400 and never a reason: telling a prober which of their
    attempts were traversal attempts buys them information and buys us
    nothing."""
    c, _, _, _ = client
    for probe in ("../../../etc/passwd", "/etc/passwd", "README.md",
                  "docs/superpowers/specs/nope.md"):
        r = c.get("/api/specs/doc",
                  params={"project": "claude-browser", "path": probe})
        assert r.status_code == 404, probe


def test_an_unknown_project_is_a_404(client):
    c, _, _, relative = client
    r = c.get("/api/specs/doc",
              params={"project": "not-a-project", "path": relative})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# The tools — the other half of the conversation
# ---------------------------------------------------------------------------

def test_both_tools_are_wired_and_declared(wired):
    server, _, _ = wired
    import jarvis_mcp
    import brain
    for name in ("review_document", "approve_document"):
        assert name in server.TOOL_HANDLERS
        assert f"mcp__jarvis__{name}" in brain.ALLOWED_TOOLS
    assert {t["name"] for t in jarvis_mcp.TOOL_SPECS} == set(server.TOOL_HANDLERS)


def test_approving_acts_and_so_is_gated(wired):
    """A line in somebody else's transcript must not be able to approve a
    design. Reading one back is harmless and is not gated."""
    server, _, _ = wired
    assert "approve_document" in server.ACTING_TOOLS
    assert "review_document" not in server.ACTING_TOOLS


def test_review_document_reads_back_the_numbered_outline(wired):
    server, _, relative = wired
    said = server.tool_review_document({"project": "claude-browser",
                                        "path": relative})
    assert "1" in said and "What we agreed" in said
    assert "approv" in said.lower()


def test_review_document_reads_one_section_when_given_its_number(wired):
    server, root, _ = wired
    said = server.tool_review_document({"project": "claude-browser",
                                        "path": f"{builds.PLAN_DIR}/2026-09-03-plan.md",
                                        "section": 2})
    assert "Serve the list" in said
    assert "Read the files off disk" not in said


def test_review_document_says_so_when_the_number_is_not_there(wired):
    server, _, relative = wired
    said = server.tool_review_document({"project": "claude-browser",
                                        "path": relative, "section": 9})
    assert "9" in said
    assert "What we agreed" not in said


def test_review_document_defaults_to_the_newest_document(wired):
    server, root, _ = wired
    said = server.tool_review_document({"project": "claude-browser"})
    assert "Task 1" in said or "Task 2" in said


def test_approve_document_records_the_approval_on_disk(wired):
    server, root, relative = wired
    said = server.tool_approve_document({"project": "claude-browser",
                                         "path": relative})
    assert "approv" in said.lower()
    assert specs.approval_of(str(root), relative)["state"] == "approved"
    assert (root / specs.APPROVAL_DIR).is_dir()


def test_approve_document_refuses_a_path_it_cannot_contain(wired):
    server, root, _ = wired
    said = server.tool_approve_document({"project": "claude-browser",
                                         "path": "../../../etc/passwd"})
    assert "sir" in said.lower()
    assert not (root / specs.APPROVAL_DIR).exists()


def test_the_page_and_jarvis_are_handed_the_same_numbering(client):
    """The guarantee. Both sides come out of `specs.read_document`, so a
    number the user says lands on the same section either way."""
    c, server, root, _ = client
    plan_path = f"{builds.PLAN_DIR}/2026-09-03-plan.md"
    page = c.get("/api/specs/doc",
                 params={"project": "claude-browser", "path": plan_path}).json()
    for section in page["sections"]:
        spoken = server.tool_review_document(
            {"project": "claude-browser", "path": plan_path,
             "section": section["number"]})
        assert section["title"] in spoken, (
            f"section {section['number']} is {section['title']!r} on the page "
            f"but JARVIS read back: {spoken!r}")


# ---------------------------------------------------------------------------
# /ws/specs — a hint, never the source of truth
# ---------------------------------------------------------------------------

# The socket's whole job is to say something when a file moves. A broken one
# says nothing, and `receive_json` on a silent socket blocks forever — a test
# that hangs the suite instead of failing it. Every receive gets a deadline.
WS_DEADLINE = 5.0


def _next_message(ws):
    """The next socket message, or a failure — never a hung suite.

    A daemon thread, deliberately: a pool would join its worker at shutdown
    and put the hang back where it was taken from.
    """
    box: dict = {}

    def pull():
        try:
            box["message"] = ws.receive_json()
        except Exception as e:                      # pragma: no cover - noise
            box["error"] = e

    puller = threading.Thread(target=pull, daemon=True)
    puller.start()
    puller.join(WS_DEADLINE)
    if "message" in box:
        return box["message"]
    if "error" in box:
        raise box["error"]
    pytest.fail(f"/ws/specs said nothing in {WS_DEADLINE}s")


def test_the_socket_opens_with_a_hint_to_reconcile(client):
    c, _, _, _ = client
    with c.websocket_connect("/ws/specs") as ws:
        hello = _next_message(ws)
    assert hello["type"] == "hello"
    # No documents in the payload: the client reconciles against /api/specs.
    assert "projects" not in hello


def test_a_document_changing_on_disk_wakes_the_page(monkeypatch, client):
    """A spec is revised by JARVIS on disk, with nothing to push an event —
    so the socket watches the files and says only that something moved. The
    client reconciles against /api/specs; the hint carries no content."""
    c, server, root, relative = client
    monkeypatch.setenv("JARVIS_SPECS_POLL", "0.05")
    with c.websocket_connect("/ws/specs") as ws:
        assert _next_message(ws)["type"] == "hello"
        doc = root / relative
        doc.write_text(doc.read_text(encoding="utf-8") + "\n## Added\n\nx\n",
                       encoding="utf-8")
        message = _next_message(ws)
    assert message == {"type": "changed"}


def test_an_approval_wakes_the_page_too(monkeypatch, client):
    """Approving by voice changes no document, only a record beside it — and
    the page has to notice, or it goes on showing "awaiting"."""
    c, server, root, relative = client
    monkeypatch.setenv("JARVIS_SPECS_POLL", "0.05")
    with c.websocket_connect("/ws/specs") as ws:
        assert _next_message(ws)["type"] == "hello"
        specs.record_approval(str(root), relative)
        assert _next_message(ws)["type"] == "changed"
