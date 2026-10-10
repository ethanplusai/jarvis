"""`create_project` — JARVIS making somewhere for work to happen.

The name arrives by microphone and through an LLM, and the tool then creates a
directory with it. Two properties carry the whole feature:

It never escapes the projects root — `../evil`, `/etc/passwd`, `a/b` and
`.hidden` are refused before anything at all is created, and containment is
proved by resolving the final path rather than by trusting a string check.

And it never reuses a directory that is already there. A "create" that
silently adopts an existing tree is how somebody else's repository ends up
being edited by an unattended run.
"""

import importlib
import os
from pathlib import Path

import pytest

import project_maker


@pytest.fixture
def root(tmp_path):
    return tmp_path / "Projects"


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """A reloaded server whose projects root is a tmp_path, never ~/Projects."""
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JARVIS_PROJECTS_DIR", str(tmp_path / "Projects"))
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server as server_module
    importlib.reload(server_module)
    run_store.init_db()
    monkeypatch.setattr(server_module, "cached_projects", [])
    return server_module, tmp_path / "Projects"


# --- the happy path -------------------------------------------------------

@pytest.mark.asyncio
async def test_a_normal_name_makes_a_directory_a_repo_and_a_readme(root):
    result = await project_maker.create("chitauri", "a cost tracker",
                                        root=root)

    target = root / "chitauri"
    assert result["created"] is True
    assert target.is_dir()
    assert (target / ".git").exists(), "a project is a git repository"
    assert (target / "README.md").exists()
    readme = (target / "README.md").read_text(encoding="utf-8")
    assert "chitauri" in readme and "a cost tracker" in readme


# --- a name someone said in English ---------------------------------------
#
# Live: the user said "let's call it Tony Stark's website" and JARVIS refused
# — "letters, numbers, dashes and underscores only" — then offered
# tony-starks-website, which the user accepted. It worked, and the refusal
# was pure friction: people name things in English.

@pytest.mark.parametrize("spoken,expected", [
    ("Tony Stark's website", "tony-starks-website"),
    ("Tony Stark’s website", "tony-starks-website"),   # a smart apostrophe
    ("My App", "my-app"),
    ("chitauri", "chitauri"),
    ("chi tauri", "chi-tauri"),
    ("The Big  Redesign!", "the-big-redesign"),
    ("Search Engine Coach (v2)", "search-engine-coach-v2"),
    ("ChiTauri", "chitauri"),
])
def test_a_spoken_name_is_slugified(spoken, expected):
    assert project_maker.sanitise_name(spoken) == expected


@pytest.mark.asyncio
async def test_the_slug_is_what_lands_on_disk_and_what_is_said(wired):
    server, root = wired
    out = await server.tool_create_project({"name": "Tony Stark's website"})
    assert (root / "tony-starks-website").is_dir()
    assert "tony-starks-website" in out, "he names what he actually made"


@pytest.mark.parametrize("bad", [
    "../evil", "..", "../../etc", "/etc/passwd", "a/b", "a\\b",
    ".hidden", ".", "", "   ", "~/elsewhere", "chitauri/../../evil",
    "x" * 200, "..evil", "./x",
])
def test_slugifying_never_repairs_a_dangerous_name(bad):
    """The dangerous shapes are refused OUTRIGHT, never slugified away.
    Turning `../evil` into `evil` would create a project under a name nobody
    said, which is worse than refusing."""
    with pytest.raises(project_maker.BadName):
        project_maker.sanitise_name(bad)


def test_a_name_that_slugs_away_to_nothing_is_refused():
    for junk in ("!!!", "---", "?"):
        with pytest.raises(project_maker.BadName):
            project_maker.sanitise_name(junk)


@pytest.mark.asyncio
async def test_a_spoken_name_with_a_space_becomes_one_directory(root):
    result = await project_maker.create("cost flex", root=root)
    assert result["name"] == "cost-flex"
    assert (root / "cost-flex").is_dir()
    # And emphatically NOT two nested directories, or one called "cost".
    assert not (root / "cost").exists()


# --- never reuse, never overwrite ----------------------------------------

@pytest.mark.asyncio
async def test_a_duplicate_name_refuses_and_leaves_the_directory_alone(root):
    root.mkdir(parents=True)
    existing = root / "chitauri"
    existing.mkdir()
    (existing / "important.txt").write_text("somebody else's work", encoding="utf-8")
    before = sorted(p.name for p in existing.iterdir())

    result = await project_maker.create("chitauri", root=root)

    assert result["created"] is False and result["reason"] == "exists"
    assert sorted(p.name for p in existing.iterdir()) == before
    assert (existing / "important.txt").read_text(encoding="utf-8") == "somebody else's work"
    assert not (existing / "README.md").exists(), "nothing was written into it"
    assert not (existing / ".git").exists(), "it was not turned into a repo"


@pytest.mark.asyncio
async def test_a_name_taken_by_a_file_is_refused_too(root):
    root.mkdir(parents=True)
    (root / "chitauri").write_text("not a directory", encoding="utf-8")

    result = await project_maker.create("chitauri", root=root)

    assert result["created"] is False
    assert (root / "chitauri").read_text(encoding="utf-8") == "not a directory"


# --- the names that must never become a path -----------------------------

@pytest.mark.parametrize("bad", [
    "../evil",
    "..",
    "../../etc",
    "/etc/passwd",
    "a/b",
    "a\\b",
    ".hidden",
    ".",
    "",
    "   ",
    "~/elsewhere",
    "chitauri/../../evil",
    "x" * 200,
])
@pytest.mark.asyncio
async def test_a_dangerous_name_is_refused_before_anything_is_created(bad, root):
    with pytest.raises(project_maker.BadName):
        await project_maker.create(bad, root=root)
    # Not even the root: the name is rejected before any mkdir happens, so a
    # refused request leaves no trace at all.
    assert not root.exists()


@pytest.mark.asyncio
async def test_the_traversal_never_reaches_the_parent(tmp_path):
    """The point of the guard, stated as the outcome it prevents."""
    root = tmp_path / "Projects"
    root.mkdir()
    outside = tmp_path / "evil"

    with pytest.raises(project_maker.BadName):
        await project_maker.create("../evil", root=root)

    assert not outside.exists(), "it escaped the projects root"


def test_containment_is_proved_by_resolving_not_by_string_matching(tmp_path):
    """`target_for` is the second, independent guard. It must refuse a name
    that would land anywhere other than directly inside the root, even if
    something upstream let it through."""
    root = tmp_path / "Projects"
    root.mkdir()
    for bad in ("../evil", "a/b", "/etc/passwd", ".."):
        with pytest.raises(project_maker.BadName):
            project_maker.target_for(bad, root)


def test_a_symlinked_root_still_compares_equal(tmp_path):
    """A symlink anywhere in the root's own path resolves on both sides, so
    it cannot make the containment check fail for a legitimate name."""
    real = tmp_path / "real-projects"
    real.mkdir()
    link = tmp_path / "Projects"
    try:
        link.symlink_to(real)
    except OSError as e:                 # Windows without the privilege
        pytest.skip(f"cannot create symlinks here ({e})")

    target = project_maker.target_for("chitauri", link)

    assert target == Path(os.path.realpath(str(real))) / "chitauri"


def test_the_root_comes_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_PROJECTS_DIR", str(tmp_path / "elsewhere"))
    assert project_maker.projects_root() == tmp_path / "elsewhere"
    monkeypatch.delenv("JARVIS_PROJECTS_DIR")
    assert project_maker.projects_root() == Path.home() / "Projects"


# --- the tool, and what the user hears -----------------------------------

@pytest.mark.asyncio
async def test_the_tool_says_where_it_put_it(wired):
    server, root = wired
    out = await server.tool_create_project({"name": "chitauri"})
    assert "chitauri" in out and root.name in out
    assert (root / "chitauri" / ".git").exists()


@pytest.mark.asyncio
async def test_the_tool_refuses_a_duplicate_out_loud(wired):
    server, root = wired
    await server.tool_create_project({"name": "chitauri"})
    out = await server.tool_create_project({"name": "chitauri"})
    assert "already" in out.lower()
    assert out.rstrip().endswith(".")


@pytest.mark.asyncio
async def test_the_tool_refuses_a_dangerous_name_in_a_speakable_sentence(wired):
    server, root = wired
    for bad in ("../evil", "/etc/passwd", "a/b", ".hidden"):
        out = await server.tool_create_project({"name": bad})
        assert "can't use that" in out
        assert not root.exists() or list(root.iterdir()) == []


@pytest.mark.asyncio
async def test_an_empty_name_asks_rather_than_creating(wired):
    server, root = wired
    out = await server.tool_create_project({"name": "   "})
    assert out.rstrip().endswith("?")
    assert not root.exists()


# --- the whole point: it is startable at once ----------------------------

@pytest.mark.asyncio
async def test_the_new_project_is_immediately_startable(wired):
    """`create_project` then `spawn_run` is the flow the user asked for. If
    the project is not resolvable the instant it exists, JARVIS answers "I
    don't see a project called that" about a directory he just made."""
    server, root = wired

    await server.tool_create_project({"name": "chitauri"})
    name, path, problem = server._resolve_project_or_explain("chitauri")

    assert problem is None
    assert name == "chitauri"
    assert path == str(root / "chitauri")


@pytest.mark.asyncio
async def test_a_rescan_keeps_the_new_project(wired):
    """Registering it in the cache is not enough on its own: the next
    /api/projects rescan replaces that cache wholesale, and the scan has to
    look in the projects root or the project silently disappears."""
    server, root = wired
    await server.tool_create_project({"name": "chitauri"})

    scanned = await server.scan_projects()

    assert str(root / "chitauri") in [p["path"] for p in scanned]


# --- the registries agree ------------------------------------------------

def test_create_project_is_registered_and_gated(wired):
    server, _root = wired
    assert "create_project" in server.TOOL_HANDLERS
    assert "create_project" in server.ACTING_TOOLS, (
        "it writes a directory into the user's filesystem")


def test_the_three_tool_sets_still_agree(wired):
    import brain
    import jarvis_mcp
    server, _root = wired
    assert "mcp__jarvis__create_project" in brain.ALLOWED_TOOLS
    assert {t["name"] for t in jarvis_mcp.TOOL_SPECS} == set(server.TOOL_HANDLERS)
    assert {t for t in brain.ALLOWED_TOOLS if t.startswith("mcp__")} <= {
        f"mcp__jarvis__{n}" for n in server.TOOL_HANDLERS}


@pytest.mark.asyncio
async def test_a_watcher_turn_cannot_create_a_project(wired, monkeypatch):
    """The gate lives in /internal/tool, so it is exercised through it."""
    from fastapi.testclient import TestClient
    import data_paths
    server, root = wired

    class _Brain:
        current_origin = "watcher"
        ready = False

        async def stop(self):
            pass

    token = data_paths.ensure_tool_token()
    with TestClient(server.app) as client:
        monkeypatch.setattr(server, "brain_instance", _Brain())
        r = client.post("/internal/tool",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"tool": "create_project",
                              "arguments": {"name": "evil"}})

    assert r.json()["ok"] is False
    assert "not_allowed_from_event" in r.json()["text"]
    assert not (root / "evil").exists()
