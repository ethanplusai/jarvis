"""A path chosen AFTER containment was decided is a path nobody checked.

`_inside_a_project` and `repo_read.resolve_within` both realpath their
candidate and compare realpaths, so a symlink cannot make the comparison
lie. That is a decision about ONE path. `open_in_browser` then took the
answer and replaced it:

    if resolved.is_dir():
        index = next((resolved / n for n in _DIRECTORY_INDEXES
                      if (resolved / n).is_file()), None)
        resolved = index          # unresolved, and never re-contained
    ...
    if _too_private_to_open(resolved):

— so both walls judged the LINK and not its target. Executed:
`site/index.html` symlinked to `<data>/jarvis/mcp.json` (the loopback tool
token's path and a verbatim copy of every token in the user's
`connections.json`) came back "Opened index.html from demo, sir." Naming the
file is refused; naming its directory was not.

`repo_read.private_reason`'s own docstring already said whose job this is:
"The caller is responsible for having resolved symlinks first where that
matters."

So the universe here is not a file and not a function name. It is:

    every `*.py` at the top level of the repository, every function in which
    a name is bound — directly, or through a rebind or a tuple-unpack — to
    the result of `_inside_a_project`, `repo_read.resolve_within` or
    `project_maker.target_for`, and a NEW path is then derived from that
    name by `/`, `.joinpath`, `.glob`, `.rglob` or `.iterdir`.

Each one is driven below with a real symlink planted at the derived path, or
carries a written reason. A function that starts deriving a path after
containment next year is in the class the moment it does.
"""

import ast
import asyncio
import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

REPO = Path(__file__).parent.parent
MODULES = sorted(REPO.glob("*.py"))

# The three functions in this repository that DECIDE containment. Each
# realpaths both sides and returns the real path; everything after them is
# somebody else's problem, which is the bug this file is about.
DECIDERS = {"_inside_a_project", "resolve_within", "target_for"}
DERIVERS = ("joinpath", "glob", "rglob", "iterdir")


def _is_decider_call(node) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = (func.id if isinstance(func, ast.Name)
            else func.attr if isinstance(func, ast.Attribute) else None)
    if name in DECIDERS:
        return True
    # `await asyncio.to_thread(repo_read.resolve_within, root, target)` —
    # the shape both repo readers use, and the one a naive walk misses.
    if name == "to_thread" and node.args:
        first = node.args[0]
        inner = (first.id if isinstance(first, ast.Name)
                 else first.attr if isinstance(first, ast.Attribute) else None)
        return inner in DECIDERS
    return False


def _unwrap(node):
    while isinstance(node, ast.Await):
        node = node.value
    return node


def _contained_names(fn) -> set:
    """Every local name holding a path containment has approved.

    A fixpoint rather than one pass, because the answer travels: in
    `open_in_browser` it is `found = _inside_a_project(...)` and then
    `project_name, resolved = found`, so a walk that only looks at the
    assignment holding the call sees nothing at all.
    """
    names: set = set()
    for _ in range(6):
        before = set(names)
        for sub in ast.walk(fn):
            if not isinstance(sub, ast.Assign):
                continue
            rhs = _unwrap(sub.value)
            carried = _is_decider_call(rhs)
            if not carried and isinstance(rhs, ast.Name) and rhs.id in names:
                carried = True
            if (not carried and isinstance(rhs, ast.Subscript)
                    and isinstance(rhs.value, ast.Name)
                    and rhs.value.id in names):
                carried = True
            if not carried:
                continue
            for target in sub.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, ast.Tuple):
                    names.update(e.id for e in target.elts
                                 if isinstance(e, ast.Name))
        if names == before:
            break
    return names


def _sites() -> dict:
    """{`module.function`: [derivations]} for the whole class."""
    out = {}
    for path in MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            contained = _contained_names(fn)
            if not contained:
                continue
            derived = set()
            for sub in ast.walk(fn):
                if (isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Div)
                        and isinstance(sub.left, ast.Name)
                        and sub.left.id in contained):
                    derived.add((sub.lineno, ast.unparse(sub)))
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr in DERIVERS
                        and isinstance(sub.func.value, ast.Name)
                        and sub.func.value.id in contained):
                    derived.add((sub.lineno, ast.unparse(sub)))
            if derived:
                out[f"{path.stem}.{fn.name}"] = sorted(derived)
    return out


SITES = _sites()

EXEMPT = {
    "project_maker.create": (
        "`target / 'README.md'` is written into a directory this function "
        "has just created with `target.mkdir(exist_ok=False)` — the atomic "
        "guard, and the one that cannot be raced: an existing directory is "
        "never adopted, so there is no pre-existing link at that path to "
        "follow. It also WRITES rather than reads, so there is no private "
        "file it could disclose."),
}


def test_the_walk_finds_the_site_the_audit_found():
    """A walk that finds nothing passes vacuously, and this one is
    load-bearing: it exists because `resolved / n` in `open_in_browser` was
    invisible to every check in the tree."""
    assert "server.tool_open_in_browser" in SITES, sorted(SITES)
    assert SITES["server.tool_open_in_browser"], SITES


def test_every_derived_path_is_driven_or_justified():
    undecided = sorted(set(SITES) - set(DRIVERS) - set(EXEMPT))
    assert not undecided, (
        f"these choose a path after containment was decided and nobody has "
        f"planted a symlink at it: {undecided}")
    stale = sorted(set(EXEMPT) - set(SITES))
    assert not stale, f"exempted but no longer derives a path: {stale}"


def test_every_exemption_is_justified_in_words():
    for name, reason in EXEMPT.items():
        assert isinstance(reason, str) and len(reason) > 60, (name, reason)


# --- the behaviour, against a real symlink on a real disk ----------------

@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setenv("JARVIS_PROJECT_ROOTS", str(projects))
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server as server_module
    importlib.reload(server_module)
    run_store.init_db()
    server_module._projects_root_for_test = projects
    return server_module


def _project_with_an_index_link(server, tmp_path, points_at: Path):
    """A project holding `site/index.html`, which is a symlink."""
    projects = server._projects_root_for_test
    demo = projects / "demo"
    (demo / "site").mkdir(parents=True)
    link = demo / "site" / "index.html"
    link.symlink_to(points_at)
    server.cached_projects[:] = [
        {"name": "demo", "path": str(demo), "branch": ""}]
    return demo, link


def _opened(server, monkeypatch) -> list:
    opened = []

    async def _open(url, which=None):
        opened.append(url)
        return {"success": True, "confirmation": "ok"}

    monkeypatch.setattr(server.actions, "open_browser", _open)
    return opened


def _drive_open_in_browser(server, monkeypatch, tmp_path, secret: Path) -> str:
    _project_with_an_index_link(server, tmp_path, secret)
    opened = _opened(server, monkeypatch)
    out = asyncio.run(server.tool_open_in_browser(
        {"target": "site", "project": "demo"}))
    return out + " || opened=" + repr(opened)


DRIVERS = {
    "server.tool_open_in_browser": _drive_open_in_browser,
}


def test_a_directory_index_that_is_a_link_to_jarviss_own_data_is_refused(
        server, monkeypatch, tmp_path):
    """The audit's exact reproduction: `<data>/jarvis/mcp.json` holds the
    loopback tool token's path and a verbatim copy of every `env` block in
    the user's `connections.json` — their Notion token, their GitHub token.
    Chrome would put it on the screen, where `look_at_screen` reads it."""
    import data_paths
    secret = data_paths.brain_home() / "mcp.json"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text('{"token": "sk-not-a-real-one"}', encoding="utf-8")

    out = _drive_open_in_browser(server, monkeypatch, tmp_path, secret)
    assert "opened=[]" in out, out
    assert server.REPO_SENSITIVE_REFUSAL.split(",")[0] in out or \
        "not opened" in out or "isn't inside" in out, out


def test_a_directory_index_that_is_a_link_out_of_every_project_is_refused(
        server, monkeypatch, tmp_path):
    """Containment is re-decided too, not only the private-file wall. The
    link may point anywhere on the disk — `~/.ssh/id_rsa` is the case
    `_too_private_to_open` was written for, and a plain file outside every
    project is the case only containment catches."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    secret = outside / "notes.txt"
    secret.write_text("private", encoding="utf-8")

    out = _drive_open_in_browser(server, monkeypatch, tmp_path, secret)
    assert "opened=[]" in out, out


def test_naming_the_file_and_naming_its_directory_agree(server, monkeypatch,
                                                        tmp_path):
    """The finding in one sentence: the two spellings of the same request
    gave different answers. They must give the same one."""
    import data_paths
    secret = data_paths.brain_home() / "mcp.json"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("{}", encoding="utf-8")
    _project_with_an_index_link(server, tmp_path, secret)
    opened = _opened(server, monkeypatch)

    by_file = asyncio.run(server.tool_open_in_browser(
        {"target": "site/index.html", "project": "demo"}))
    by_dir = asyncio.run(server.tool_open_in_browser(
        {"target": "site", "project": "demo"}))
    assert not opened, (by_file, by_dir, opened)


def test_an_ordinary_directory_index_still_opens(server, monkeypatch,
                                                 tmp_path):
    """The other half. A wall that refuses the real thing is not a fix:
    `open_in_browser("site")` on a project's own `site/index.html` is the
    request this branch exists to serve."""
    projects = server._projects_root_for_test
    demo = projects / "demo"
    (demo / "site").mkdir(parents=True)
    (demo / "site" / "index.html").write_text("<h1>hello</h1>", encoding="utf-8")
    server.cached_projects[:] = [
        {"name": "demo", "path": str(demo), "branch": ""}]
    opened = _opened(server, monkeypatch)

    out = asyncio.run(server.tool_open_in_browser(
        {"target": "site", "project": "demo"}))
    assert opened, out
    assert opened[0].endswith("index.html"), opened
    assert "Opened index.html" in out, out


def test_an_index_that_is_a_link_INSIDE_the_project_still_opens(
        server, monkeypatch, tmp_path):
    """A symlink is not itself the problem — a `site/index.html` linked to
    the project's own `build/index.html` is an ordinary way to lay out a
    repository, and re-resolving must not break it."""
    projects = server._projects_root_for_test
    demo = projects / "demo"
    (demo / "build").mkdir(parents=True)
    real = demo / "build" / "index.html"
    real.write_text("<h1>built</h1>", encoding="utf-8")
    (demo / "site").mkdir()
    (demo / "site" / "index.html").symlink_to(real)
    server.cached_projects[:] = [
        {"name": "demo", "path": str(demo), "branch": ""}]
    opened = _opened(server, monkeypatch)

    out = asyncio.run(server.tool_open_in_browser(
        {"target": "site", "project": "demo"}))
    assert opened, out
    assert opened[0].endswith("index.html"), opened
