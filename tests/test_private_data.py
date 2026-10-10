"""JARVIS's own data directory is not a readable part of any project.

`data/` defaults to a directory INSIDE JARVIS's own repository, and
`_repo_project` resolves "yourself" to that repository. So `read_file` on his
own source reached the whole of it. Confirmed live before this was closed:

    tool_read_file({"project": "jarvis", "path": "data/jarvis/mcp.json"})

came back with the tool-token file's path and every `env` block the user had
written into `connections.json` — which is the file the documentation tells
them to paste credentials into. `read_file` is not an acting tool, so no
origin gate stood in the way, and until the previous commit no taint did
either; `WebFetch` is the CLI's own ungated tool, so the exfiltration leg was
free.

`repo_read.sensitive_reason` refused `tool-token` by exact name and nothing
else in there. `mcp.json`, `connections.json`, `MEMORY.md`, `memory/`,
`projects/`, `journal/` and `jarvis.db` were all readable.

The wall is by ABSOLUTE PATH, not by name: a project may legitimately hold
its own `mcp.json` or `MEMORY.md` and the user is entitled to ask about
those. `test_a_projects_own_mcp_json_is_still_readable` is the half of this
that stops the fix from being a blunt name ban.
"""

import importlib
import json
import os
import stat
from pathlib import Path

import sys

import pytest

SECRET = "sk-live-notion-token-do-not-read-me"


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """A project that CONTAINS JARVIS's data directory — the live layout.

    `data_dir()` defaults to `<repo>/data`, so on a real install the brain
    home sits inside the very repository `read_file` can open.
    """
    project = tmp_path / "jarvis"
    project.mkdir()
    data = project / "data"
    monkeypatch.setenv("JARVIS_DATA_DIR", str(data))
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")

    import data_paths
    importlib.reload(data_paths)
    import repo_read as repo_read_module
    importlib.reload(repo_read_module)
    import run_store
    importlib.reload(run_store)
    import jarvis_memory
    importlib.reload(jarvis_memory)
    import server as server_module
    importlib.reload(server_module)
    run_store.init_db()

    home = jarvis_memory.ensure_layout()
    (home / "mcp.json").write_text(json.dumps(
        {"mcpServers": {"notion": {"command": "npx",
                                   "env": {"NOTION_TOKEN": SECRET}}}}))
    (home / "connections.json").write_text(json.dumps(
        {"notion": {"command": "npx", "env": {"NOTION_TOKEN": SECRET}}}))
    (home / "MEMORY.md").write_text(f"- [a memory](x.md) — {SECRET}\n")
    (data_paths.memory_dir() / "x.md").write_text(f"# x\n\n{SECRET}\n")
    (data_paths.journal_dir() / "j.md").write_text(f"# j\n\n{SECRET}\n")
    (data_paths.projects_dir() / "p.md").write_text(f"# p\n\n{SECRET}\n")

    # Something ordinary in the same project, so a refusal that refuses
    # everything would be visible.
    (project / "README.md").write_text("# JARVIS\n\nA voice assistant.\n")
    (project / "server.py").write_text("PORT = 8340\n")

    monkeypatch.setattr(server_module, "cached_projects",
                        [{"name": "jarvis-repo", "path": str(project)}])
    return server_module, project, data_paths


PRIVATE = [
    "data/jarvis/mcp.json",
    "data/jarvis/connections.json",
    "data/jarvis/MEMORY.md",
    "data/jarvis/memory/x.md",
    "data/jarvis/journal/j.md",
    "data/jarvis/projects/p.md",
    "data/jarvis/tool-token",
]


# --- the reviewer's own call ---------------------------------------------

@pytest.mark.parametrize("relative", PRIVATE)
@pytest.mark.asyncio
async def test_read_file_refuses_jarvis_own_data(wired, relative):
    server, _project, _dp = wired
    out = await server.tool_read_file({"project": "jarvis-repo",
                                       "path": relative})
    assert SECRET not in out, out
    assert out == server.REPO_SENSITIVE_REFUSAL, out


@pytest.mark.asyncio
async def test_the_ordinary_files_in_that_project_still_read(wired):
    """A wall that refuses the whole project is not a wall, it is a bug."""
    server, _project, _dp = wired
    out = await server.tool_read_file({"project": "jarvis-repo",
                                       "path": "server.py"})
    assert "PORT = 8340" in out, out


@pytest.mark.asyncio
async def test_search_never_surfaces_a_line_from_the_brain_home(wired,
                                                                monkeypatch):
    """The named-path refusal is not enough on its own: a grep would have
    read the token out line by line without ever naming the file."""
    import repo_read
    server, _project, _dp = wired
    monkeypatch.setattr(repo_read, "_rg_path", lambda: None)   # the pure walk
    out = await server.tool_search_repo({"project": "jarvis-repo",
                                         "query": SECRET})
    # "Nothing matching that" no longer echoes the query (it is the brain's
    # own argument, tests/test_tool_argument_echo.py) — what must not appear
    # is a FILE, a line number, or the untrusted block a hit would arrive in.
    assert "Nothing matching" in out, out
    assert "mcp.json" not in out and "session-output" not in out, out


@pytest.mark.asyncio
async def test_ripgrep_takes_the_same_wall(wired):
    """Whichever engine is installed, the answer is the same. Skipped where
    ripgrep is not on PATH — there the walk above IS the live path."""
    import repo_read
    server, _project, _dp = wired
    if repo_read._rg_path() is None:
        pytest.skip("no ripgrep on this machine")
    out = await server.tool_search_repo({"project": "jarvis-repo",
                                         "query": SECRET})
    assert "Nothing matching" in out, out


@pytest.mark.asyncio
async def test_the_overview_does_not_list_the_brain_home(wired):
    server, _project, _dp = wired
    out = await server.tool_repo_overview({"project": "jarvis-repo"})
    assert "mcp.json" not in out, out
    assert "MEMORY.md" not in out, out


def test_the_wall_is_a_path_and_not_a_name(wired, tmp_path):
    """A project may legitimately hold its own `mcp.json` or `MEMORY.md`, and
    the user is entitled to ask about those."""
    import repo_read
    other = tmp_path / "somebody-elses-project"
    (other / "config").mkdir(parents=True)
    (other / "config" / "mcp.json").write_text("{}\n")
    (other / "MEMORY.md").write_text("# notes\n")

    for name in ("config/mcp.json", "MEMORY.md"):
        assert repo_read.resolve_within(other, name).exists(), name


def test_a_symlink_into_the_brain_home_is_refused(wired, tmp_path):
    """Both sides are resolved, so a link out of the project cannot make the
    comparison lie — the same reasoning `resolve_within` already used for
    containment."""
    import repo_read
    server, project, dp = wired
    link = project / "innocent.json"
    try:
        link.symlink_to(dp.brain_home() / "mcp.json")
    except OSError:
        pytest.skip("cannot create symlinks here")
    with pytest.raises(repo_read.Refused):
        repo_read.resolve_within(project, "innocent.json")


# --- the file the credentials are copied INTO ----------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes; Windows uses ACLs")
def test_the_generated_mcp_config_is_not_world_readable(wired):
    """`_write_mcp_config` copies every `env` block out of the user's
    `connections.json` — their Notion token, their GitHub token — into
    `mcp.json`, and wrote it at the default umask (`-rw-r--r--`). The token
    file beside it has been 0600 since it was created; this had no reason to
    be looser."""
    server, _project, dp = wired
    path = server._write_mcp_config(dp.brain_home())
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, oct(mode)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes; Windows uses ACLs")
def test_a_pre_existing_mcp_config_has_its_mode_forced_back(wired):
    """Adopting a file somebody else created with looser permissions would
    keep their read access — the same rule `ensure_tool_token` already
    applies to the token."""
    server, _project, dp = wired
    path = dp.brain_home() / "mcp.json"
    path.write_text("{}")
    os.chmod(path, 0o644)
    server._write_mcp_config(dp.brain_home())
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# --- opening things in a browser -----------------------------------------

@pytest.mark.asyncio
async def test_open_in_browser_refuses_a_private_file(monkeypatch, tmp_path):
    """It applied containment and never the sensitive-file wall, so with the
    user's home as a project it would put `~/.ssh/id_rsa` on screen in
    Chrome — where `look_at_screen` then reads it back."""
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server as server_module
    importlib.reload(server_module)
    run_store.init_db()

    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
    (home / "index.html").write_text("<h1>fine</h1>")

    opened: list[str] = []

    class _Actions:
        async def open_browser(self, url, browser="chrome"):
            opened.append(url)
            return {"success": True, "confirmation": "Pulled that up, sir."}

    monkeypatch.setattr(server_module, "actions", _Actions())
    monkeypatch.setattr(server_module, "cached_projects",
                        [{"name": "home", "path": str(home)}])

    said = await server_module.tool_open_in_browser(
        {"target": ".ssh/id_rsa", "project": "home"})
    assert opened == [], f"it opened {opened}"
    assert said == server_module.REPO_SENSITIVE_REFUSAL, said

    # And the ordinary file in the same project still opens, or the refusal
    # above proves nothing.
    await server_module.tool_open_in_browser(
        {"target": "index.html", "project": "home"})
    assert len(opened) == 1 and opened[0].endswith("index.html"), opened


@pytest.mark.asyncio
async def test_open_in_browser_refuses_jarvis_own_data(wired, monkeypatch):
    server, project, _dp = wired
    opened: list[str] = []

    class _Actions:
        async def open_browser(self, url, browser="chrome"):
            opened.append(url)
            return {"success": True, "confirmation": "Pulled that up, sir."}

    monkeypatch.setattr(server, "actions", _Actions())
    said = await server.tool_open_in_browser(
        {"target": "data/jarvis/mcp.json", "project": "jarvis-repo"})
    assert opened == [], f"it opened {opened}"
    assert said == server.REPO_SENSITIVE_REFUSAL, said


# --- the wall is about IDENTITY, not spelling ----------------------------
#
# `private_reason` compared two `PosixPath`s for equality. Both sides come
# out of `os.path.realpath`, and macOS's `realpath` does not case-normalise
# on a case-insensitive volume: it resolves the symlinks and hands back
# whatever spelling it was given. So the OS happily opened the file and the
# guard, looking at the string, decided it "wasn't under" the private root.
# Confirmed live against the default layout before this was closed:
#
#     refused  'data/jarvis/mcp.json'  -> sensitive
#     ALLOWED  'DATA/jarvis/mcp.json'  exists=True bytes=485
#
# The class of input is not "the uppercase one". It is EVERY SPELLING THE
# FILESYSTEM WILL ACCEPT for the same directory — upper, lower, mixed,
# reached through a `..`, named absolutely, or reached through a symlink
# whose TARGET is the mis-cased one. The old test enumerated one spelling;
# that is why it passed against a hole. So this one is GENERATED rather than
# typed: `_spellings` derives the variants from the name itself, and every
# private path is run through every one of them.


def _spellings(name: str) -> list[str]:
    """Every casing of one path component. Generated, not hand-listed."""
    alternating = "".join(c.upper() if i % 2 else c.lower()
                          for i, c in enumerate(name))
    return sorted({name, name.upper(), name.lower(), name.capitalize(),
                   alternating})


def _recased(relative: str, spelling: str) -> str:
    """`relative` with its first component respelled."""
    _head, _, tail = relative.partition("/")
    return f"{spelling}/{tail}" if tail else spelling


CASE_VARIANTS = [(rel, spelling)
                 for rel in PRIVATE
                 for spelling in _spellings("data")]


@pytest.mark.parametrize("relative,spelling", CASE_VARIANTS)
def test_every_spelling_of_the_private_root_is_refused(wired, relative,
                                                       spelling):
    """`resolve_within` is the choke point; it must not care about case."""
    import repo_read
    _server, project, _dp = wired
    with pytest.raises(repo_read.Refused):
        repo_read.resolve_within(project, _recased(relative, spelling))


@pytest.mark.parametrize("spelling", _spellings("data"))
@pytest.mark.asyncio
async def test_read_file_refuses_every_spelling(wired, spelling):
    """End to end, through the tool the brain actually calls."""
    server, _project, _dp = wired
    for relative in PRIVATE:
        out = await server.tool_read_file(
            {"project": "jarvis-repo", "path": _recased(relative, spelling)})
        assert SECRET not in out, out
        assert out == server.REPO_SENSITIVE_REFUSAL, (spelling, relative, out)


@pytest.mark.parametrize("template", [
    "src/../{s}/jarvis/mcp.json",
    "./{s}/./jarvis/mcp.json",
    "data/../{s}/jarvis/mcp.json",
    "{s}/jarvis/../jarvis/mcp.json",
    "{s}/jarvis/memory/../connections.json",
])
def test_a_traversal_that_rejoins_the_private_root_is_refused(wired, template):
    import repo_read
    _server, project, _dp = wired
    (project / "src").mkdir(exist_ok=True)
    for spelling in _spellings("data"):
        with pytest.raises(repo_read.Refused):
            repo_read.resolve_within(project, template.format(s=spelling))


def test_the_absolute_form_is_refused_in_every_spelling(wired):
    import repo_read
    _server, project, _dp = wired
    for spelling in _spellings("data"):
        target = str(project / spelling / "jarvis" / "mcp.json")
        with pytest.raises(repo_read.Refused):
            repo_read.resolve_within(project, target)


def test_a_symlink_whose_target_is_miscased_is_refused(wired):
    """`ln -s data link` was refused and `ln -s DATA link` was not — the
    same file, reached the same way, decided differently by a string."""
    import repo_read
    _server, project, _dp = wired
    for i, spelling in enumerate(_spellings("data")):
        link = project / f"innocent{i}"
        try:
            link.symlink_to(project / spelling / "jarvis" / "mcp.json")
        except OSError:
            pytest.skip("cannot create symlinks here")
        with pytest.raises(repo_read.Refused):
            repo_read.resolve_within(project, link.name)


def test_the_walk_never_descends_a_miscased_private_root(wired):
    """A grep does not name a file, so the named-path refusal never runs.
    `walk._is_private` carried the identical string comparison, so a project
    root reached by any other spelling made the walk read the brain home."""
    import repo_read
    _server, project, _dp = wired
    roots = [project] + [project.parent / s for s in _spellings(project.name)]
    for root in roots:
        found = repo_read.walk(root)
        leaked = [rel for rel, _ in found.files
                  if "mcp.json" in rel or "connections.json" in rel
                  or "MEMORY.md" in rel]
        assert not leaked, (root, leaked)
        # …and the walk still WORKS from that root, or it proves nothing.
        assert any(rel == "server.py" for rel, _ in found.files), root


@pytest.mark.asyncio
async def test_search_refuses_every_spelling_of_the_root(wired, monkeypatch):
    import repo_read
    server, project, _dp = wired
    monkeypatch.setattr(repo_read, "_rg_path", lambda: None)
    for spelling in _spellings(project.name):
        root = project.parent / spelling
        monkeypatch.setattr(server, "cached_projects",
                            [{"name": "jarvis-repo", "path": str(root)}])
        out = await server.tool_search_repo({"project": "jarvis-repo",
                                             "query": SECRET})
        assert "Nothing matching" in out, (spelling, out)
        assert "mcp.json" not in out, (spelling, out)


# --- the brain home reached AS a project ---------------------------------
#
# `_resolve_project_or_explain` matches by substring, so `project="jarv"`
# resolves to `<data>/jarvis` — the brain's own cwd. `repo_overview`'s
# top-level listing went through `_skip_dir`/`_skip_file` and never through
# `private_reason`, so it printed `files CLAUDE.md, connections.json,
# mcp.json`. The old test pointed at the PARENT project, where the listing
# never reaches the brain home at all, which is why it passed.


@pytest.fixture
def brain_home_as_project(wired, monkeypatch):
    server, _project, dp = wired
    home = dp.brain_home()
    (home / "CLAUDE.md").write_text("# persona\n")
    monkeypatch.setattr(server, "cached_projects",
                        [{"name": "jarvis-brain", "path": str(home)}])
    return server, home


@pytest.mark.asyncio
async def test_the_overview_of_the_brain_home_names_nothing_in_it(
        brain_home_as_project):
    server, _home = brain_home_as_project
    out = await server.tool_repo_overview({"project": "jarvis-brain"})
    for name in ("mcp.json", "connections.json", "MEMORY.md", "CLAUDE.md",
                 "memory/", "journal/", "projects/", SECRET):
        assert name not in out, (name, out)


@pytest.mark.asyncio
async def test_the_overview_still_lists_an_ordinary_project(wired):
    """A listing that lists nothing is not a fix, it is a broken tool."""
    server, _project, _dp = wired
    out = await server.tool_repo_overview({"project": "jarvis-repo"})
    assert "server.py" in out, out


@pytest.mark.asyncio
async def test_open_in_editor_refuses_the_brain_home_itself(
        brain_home_as_project, monkeypatch):
    """The empty-path branch set `resolved = realpath(root)` and applied no
    wall at all, so `{'project': 'jarv'}` opened the whole brain home."""
    server, _home = brain_home_as_project
    opened: list[str] = []

    class _Actions:
        async def open_in_editor(self, path):
            opened.append(path)
            return {"success": True, "editor": "Cursor"}

    monkeypatch.setattr(server, "actions", _Actions())
    said = await server.tool_open_in_editor({"project": "jarvis-brain"})
    assert opened == [], f"it opened {opened}"
    assert said == server.REPO_SENSITIVE_REFUSAL, said


@pytest.mark.asyncio
async def test_open_in_editor_still_opens_an_ordinary_project(wired,
                                                              monkeypatch):
    server, _project, _dp = wired
    opened: list[str] = []

    class _Actions:
        async def open_in_editor(self, path):
            opened.append(path)
            return {"success": True, "editor": "Cursor"}

    monkeypatch.setattr(server, "actions", _Actions())
    await server.tool_open_in_editor({"project": "jarvis-repo"})
    assert len(opened) == 1, opened


def test_somebody_elses_data_directory_is_not_jarviss(wired, tmp_path):
    """The wall is one absolute directory. A project of the user's own that
    happens to hold a `data/` is still theirs to read."""
    import repo_read
    other = tmp_path / "not-jarvis"
    (other / "data").mkdir(parents=True)
    (other / "data" / "notes.md").write_text("# ordinary\n")
    assert repo_read.resolve_within(other, "data/notes.md").exists()


def test_the_kernel_decides_where_the_spelling_cannot(wired):
    """The leg no case-folding can cover.

    A symlinked ancestor names the SAME directory by a path that is not a
    case variant of the real one and does not even have the same number of
    components — `/var/…` for `/private/var/…` on macOS, a firmlink for
    `/Users`, a bind mount on Linux. Case-folding is blind to all three;
    `st_dev`/`st_ino` is not. Skipped where the machine has no such alias,
    because then there is nothing to prove.
    """
    import repo_read
    _server, _project, dp = wired
    real = Path(os.path.realpath(str(dp.data_dir())))

    aliased = None
    for candidate in (Path("/var"), Path("/tmp"), Path("/etc")):
        if not candidate.is_symlink():
            continue
        resolved = os.path.realpath(str(candidate))
        if str(real).startswith(resolved + "/"):
            aliased = Path(str(candidate) + str(real)[len(resolved):])
            break
    if aliased is None:
        pytest.skip("no symlinked ancestor above the data directory here")

    # Not a case variant, and not even the same number of components.
    assert [p.casefold() for p in aliased.parts] != \
        [p.casefold() for p in real.parts], (aliased, real)
    assert repo_read.private_reason(aliased), aliased
    assert repo_read.private_reason(aliased / "jarvis" / "mcp.json"), aliased
