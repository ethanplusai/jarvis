"""`/api/memory` — the read-only window onto the brain's Markdown folder.

Two things here are load-bearing beyond "the shape is right".

First, a journal entry's `when` comes from the timestamp in its own filename,
never from mtime. The folder is the user's to edit, and correcting a typo in
an old entry updates its mtime without making it the newest entry — that
exact confusion has already cost this milestone one bug.

Second, `slug` arrives from a URL and is treated as hostile. The traversal
tests below are the whole reason `doc_path` resolves both sides and checks
containment rather than trusting a string scan.
"""

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def api(monkeypatch, tmp_path):
    """A server with an isolated JARVIS_DATA_DIR, and no real brain."""
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    import data_paths
    import jarvis_memory
    import run_store
    import server as server_module
    importlib.reload(server_module)
    with TestClient(server_module.app) as client:
        yield client, jarvis_memory, data_paths, run_store


# --- The snapshot ---------------------------------------------------------

def test_an_absent_folder_is_empty_and_not_a_404(api):
    """404 means "this route isn't wired" — which is exactly what the
    dashboard renders as "not available yet". An empty memory is a 200."""
    client, _jm, data_paths, _rs = api

    r = client.get("/api/memory")

    assert r.status_code == 200
    body = r.json()
    assert body["index"] == []
    assert body["memories"] == []
    assert body["projects"] == []
    assert body["journal"] == []
    assert body["latest_journal_slug"] is None
    assert body["path"] == str(data_paths.brain_home())


def test_a_read_never_creates_the_folder(api):
    """A GET must have no side effects.

    Startup now seeds the whole layout (the persona's `@MEMORY.md` import needs
    the index to exist from the first boot), so the folder is removed here
    first — otherwise this asserts nothing. What is under test is the READ
    path, which must never resurrect it.
    """
    import shutil
    client, _jm, data_paths, _rs = api
    shutil.rmtree(data_paths.memory_dir(), ignore_errors=True)
    assert not data_paths.memory_dir().exists(), "precondition"

    client.get("/api/memory")

    assert not data_paths.memory_dir().exists(), "a GET must not have side effects"


def test_the_snapshot_lists_memories_projects_and_the_journal(api):
    client, jm, _dp, _rs = api
    jm.write_memory("Tony prefers Postgres over SQLite", "for chitauri")
    jm.add_to_index("Tony prefers Postgres over SQLite", "database preference")
    jm.write_project_note("chitauri", "The billing job runs nightly.")
    jm.write_journal("We shipped the dashboard.", reason="shutdown")

    body = client.get("/api/memory").json()

    assert body["index"] == [{"title": "Tony prefers Postgres over SQLite",
                              "slug": "tony-prefers-postgres-over-sqlite",
                              "hook": "database preference"}]
    assert [m["slug"] for m in body["memories"]] == \
        ["tony-prefers-postgres-over-sqlite"]
    assert body["memories"][0]["title"] == "Tony prefers Postgres over SQLite"
    assert isinstance(body["memories"][0]["modified"], (int, float))
    assert [p["slug"] for p in body["projects"]] == ["chitauri"]
    assert len(body["journal"]) == 1
    assert body["journal"][0]["reason"] == "shutdown"
    assert body["latest_journal_slug"] == body["journal"][0]["slug"]


def test_journal_when_comes_from_the_filename_not_mtime(api):
    """The user edits this folder by hand. Touching an old entry must not
    reorder the journal."""
    import os
    import time

    client, jm, _dp, _rs = api
    older = jm.write_journal("the older entry", reason="shutdown")
    time.sleep(0.01)
    jm.write_journal("the newer entry", reason="rotation")
    future = time.time() + 100_000
    os.utime(older, (future, future))

    body = client.get("/api/memory").json()
    by_reason = {e["reason"]: e["when"] for e in body["journal"]}

    assert by_reason["rotation"] > by_reason["shutdown"], "mtime lied; the name did not"
    assert by_reason["shutdown"] < future - 1_000


def test_latest_journal_slug_is_the_one_the_brain_will_carry(api):
    """Placeholders are written but never handed on, so the dashboard must
    not point at one as "where he left off"."""
    client, jm, _dp, _rs = api
    jm.write_journal("the real handover", reason="shutdown")
    jm.write_journal("Session ended; the brain wrote no handover.",
                     reason="shutdown-silent")

    body = client.get("/api/memory").json()

    assert len(body["journal"]) == 2, "the placeholder is still listed"
    assert "shutdown-silent" not in body["latest_journal_slug"]
    assert body["latest_journal_slug"].endswith("-shutdown")


def test_a_hand_renamed_journal_file_does_not_break_the_listing(api):
    client, jm, data_paths, _rs = api
    jm.write_journal("a real entry", reason="shutdown")
    (data_paths.journal_dir() / "renamed-by-hand.md").write_text("# mystery\n", encoding="utf-8")

    r = client.get("/api/memory")

    assert r.status_code == 200
    assert len(r.json()["journal"]) == 1


# --- One document ---------------------------------------------------------

def test_a_memory_comes_back_as_raw_markdown(api):
    client, jm, _dp, _rs = api
    path = jm.write_memory("A fact", "the body of it")

    r = client.get("/api/memory/memory/a-fact")

    assert r.status_code == 200
    assert r.json() == {"slug": "a-fact", "text": path.read_text(encoding="utf-8")}


def test_a_long_document_is_not_truncated(api):
    client, jm, _dp, _rs = api
    jm.write_memory("A long fact", "x" * 20_000)

    text = client.get("/api/memory/memory/a-long-fact").json()["text"]

    assert text.count("x") == 20_000


@pytest.mark.parametrize("kind,slug_of", [
    ("project", lambda jm: "chitauri"),
    ("journal", lambda jm: jm.journal_entries_meta()[0]["slug"]),
])
def test_projects_and_journal_entries_are_readable(api, kind, slug_of):
    client, jm, _dp, _rs = api
    jm.write_project_note("chitauri", "nightly billing")
    jm.write_journal("we shipped it", reason="shutdown")

    r = client.get(f"/api/memory/{kind}/{slug_of(jm)}")

    assert r.status_code == 200
    assert r.json()["text"]


def test_a_missing_document_is_a_404(api):
    client, _jm, _dp, _rs = api
    assert client.get("/api/memory/memory/never-written").status_code == 404


def test_an_unknown_kind_is_a_404(api):
    client, jm, _dp, _rs = api
    jm.write_memory("A fact", "body")
    assert client.get("/api/memory/secrets/a-fact").status_code == 404


# --- Traversal ------------------------------------------------------------

TRAVERSALS = [
    "..%2f..%2f..%2fetc%2fpasswd",
    "../../../etc/passwd",
    "..",
    "....//....//etc/passwd",
    "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
    "/etc/passwd",
    "..%5c..%5cwindows%5csystem32",
]


@pytest.mark.parametrize("attempt", TRAVERSALS)
def test_a_slug_cannot_escape_its_folder_over_http(api, attempt):
    client, jm, _dp, _rs = api
    jm.write_memory("A fact", "body")            # the folder exists and is readable

    r = client.get(f"/api/memory/memory/{attempt}")

    # Some of these the HTTP client normalises away before they are ever sent
    # (".." collapses the path, and the snapshot route answers instead). What
    # must never happen is a DOCUMENT coming back — a body with "text" in it —
    # for anything outside the folder.
    assert "root:" not in r.text, f"{attempt!r} served /etc/passwd"
    if r.status_code == 200:
        assert "text" not in r.json(), f"{attempt!r} was served as a document"
    else:
        assert r.status_code in (404, 405), f"{attempt!r} was not refused"


@pytest.mark.parametrize("attempt", TRAVERSALS + ["", ".", ".hidden", "a/b", "a\\b"])
def test_doc_path_refuses_every_escape_directly(api, attempt):
    """The HTTP layer normalises some of these away before routing, so the
    guard is also pinned at the function that actually makes the decision."""
    _client, jm, _dp, _rs = api
    jm.write_memory("A fact", "body")

    for kind in ("memory", "project", "journal"):
        assert jm.doc_path(kind, attempt) is None, f"{kind}/{attempt!r} escaped"


def test_a_relative_slug_cannot_reach_a_real_markdown_file_outside(api):
    """The traversal cases above are refused twice over — the guard, and the
    fact that /etc/passwd is not a `.md` file. This one removes the second
    accident: a real Markdown file one directory up, which a missing guard
    would happily serve."""
    client, jm, data_paths, _rs = api
    jm.write_memory("A fact", "body")
    (data_paths.brain_home() / "escaped.md").write_text("the private one\n", encoding="utf-8")

    assert jm.doc_path("memory", "../escaped") is None
    assert jm.doc_path("memory", "..%2fescaped") is None
    r = client.get("/api/memory/memory/..%2fescaped")
    assert "the private one" not in r.text


def test_a_symlink_out_of_the_folder_is_refused(api):
    """A string scan cannot see this one: the slug is an ordinary name and
    the path only leaves the folder once it is resolved."""
    client, jm, data_paths, _rs = api
    jm.write_memory("A fact", "body")
    outside = data_paths.data_dir() / "secret.md"
    outside.write_text("root:x:0:0:\n", encoding="utf-8")
    (data_paths.memory_dir() / "innocent.md").symlink_to(outside)

    assert jm.doc_path("memory", "innocent") is None
    r = client.get("/api/memory/memory/innocent")
    assert r.status_code == 404
    assert "root:" not in r.text


def test_a_real_file_is_still_served_after_all_that(api):
    """The guard must refuse traversal without refusing the ordinary case."""
    client, jm, _dp, _rs = api
    jm.write_memory("A fact", "body")
    assert client.get("/api/memory/memory/a-fact").status_code == 200


# --- the index and the folder can disagree, and the page must say so -------

def test_the_snapshot_names_memories_the_index_does_not(api):
    client, jm, _dp, _rs = api
    jm.write_memory("StarNet station and how to work it", "Start with starnet_status.")
    jm.write_memory("Tony prefers Postgres", "for chitauri")
    jm.add_to_index("Tony prefers Postgres", "database preference")

    body = client.get("/api/memory").json()

    assert [u["slug"] for u in body["unindexed"]] == ["starnet-station-and-how-to-work-it"]
    assert body["unindexed"][0]["title"] == "StarNet station and how to work it"


def test_reindex_over_http_adds_the_missing_lines(api):
    client, jm, data_paths, _rs = api
    jm.write_memory("StarNet station and how to work it", "Start with starnet_status.")
    token = data_paths.ensure_tool_token()

    r = client.post("/api/memory/reindex",
                    headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 200
    assert r.json() == {"indexed": ["starnet-station-and-how-to-work-it"],
                        "left_out": [], "full": False}
    after = client.get("/api/memory").json()
    assert after["unindexed"] == []
    assert [e["slug"] for e in after["index"]] == ["starnet-station-and-how-to-work-it"]


def test_reindex_is_a_mutation_and_sits_behind_the_web_boundary(api):
    """No Origin, no token: not a browser JARVIS serves and not his own
    tool child, so it is refused like every other state change."""
    client, jm, _dp, _rs = api
    jm.write_memory("StarNet station", "body")

    r = client.post("/api/memory/reindex")

    assert r.status_code in (401, 403)
    assert jm.unindexed_memories(), "a refused call must not have written"
