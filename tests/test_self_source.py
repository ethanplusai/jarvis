"""JARVIS can read the one repository he could not: his own.

The user, twice: "Jarvis how much info do you have about how you are built",
and "but couldn't you technically look at your own Jarvis repo".

He had `repo_overview`, `search_repo` and `read_file` and no idea that one of
the repositories on this machine was him. This wires his own source in as a
project those three answer to — and nothing more than those three, because
JARVIS reading his own code is the ask and JARVIS starting an unattended
Claude Code process that EDITS his own code while running on it is not.

These tests read the REAL repository, deliberately: the point is that it works
with nothing configured, on a fresh clone, on the user's other machine. What
they must never do is read a secret out of it, so the .env refusal is checked
against the real file rather than a fixture — a fixture is exactly how this
project has previously fooled itself into thinking a guard was live.
"""

import importlib
import os
from pathlib import Path

import pytest


@pytest.fixture
def ready(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server as server_module
    importlib.reload(server_module)
    run_store.init_db()
    # No projects at all: everything below must work off the self-reference,
    # not off a session that happens to be open on the jarvis directory.
    monkeypatch.setattr(server_module, "cached_projects", [])
    return server_module


REPO = Path(__file__).resolve().parents[1]


# --- where his own root comes from ----------------------------------------

def test_his_own_root_is_derived_from_the_code_not_configured(ready):
    """Not a hard-coded path and not a setting: it must be right on the user's
    other machine, freshly cloned, with nobody setting anything — and inside
    a git worktree, which is where this very test is running."""
    server = ready
    assert server._jarvis_source_root() == Path(
        os.path.realpath(os.path.dirname(server.__file__)))
    assert (server._jarvis_source_root() / "server.py").is_file()


def test_the_root_ignores_the_environment(ready, monkeypatch):
    monkeypatch.setenv("JARVIS_DATA_DIR", "/nowhere")
    monkeypatch.setenv("JARVIS_PROJECTS_DIR", "/nowhere")
    server = ready
    assert (server._jarvis_source_root() / "server.py").is_file()


# --- what he answers to ----------------------------------------------------

@pytest.mark.parametrize("said", [
    "jarvis", "JARVIS", "Jarvis.", "yourself", "your own code", "your repo",
    "your source code",
])
def test_he_answers_to_the_things_the_user_actually_says(ready, said):
    server = ready
    got = server._repo_project({"project": said})
    assert not isinstance(got, str), got
    name, root = got
    assert name == server.JARVIS_SELF_NAME
    assert root == server._jarvis_source_root()


@pytest.mark.parametrize("said", ["jarvis-dashboard", "jarvisify", "yourselves",
                                  "the jarvis frontend"])
def test_a_name_that_merely_contains_jarvis_is_not_him(ready, said):
    """Matched exactly, never as a substring — a real project called
    jarvis-dashboard must not silently resolve to his own source."""
    server = ready
    got = server._repo_project({"project": said})
    assert isinstance(got, str), f"{said!r} resolved to {got!r}"


# --- reading himself -------------------------------------------------------

@pytest.mark.asyncio
async def test_he_can_describe_what_he_is(ready):
    server = ready
    answer = await server.tool_repo_overview({"project": "jarvis"})
    assert "JARVIS" in answer
    assert 'untrusted="true"' in answer, "his own README is still content"


@pytest.mark.asyncio
async def test_he_can_find_where_something_lives_in_himself(ready):
    server = ready
    answer = await server.tool_search_repo({"project": "yourself",
                                            "query": "BLOCKING_RATE_LIMIT_STATUSES"})
    assert "brain.py" in answer


@pytest.mark.asyncio
async def test_he_can_read_his_own_source(ready):
    server = ready
    answer = await server.tool_read_file({"project": "jarvis",
                                          "path": "run_store.py",
                                          "around": "def init_db"})
    assert "run_store.py, lines" in answer
    assert "def init_db" in answer


# --- and is refused exactly where anyone else is --------------------------

@pytest.mark.asyncio
async def test_his_own_env_is_as_private_as_anybody_elses(ready):
    """His .env holds the Fish Audio key. The wall is `repo_read`'s, reused
    rather than rewritten — this test proves it is actually in the path."""
    server = ready
    answer = await server.tool_read_file({"project": "jarvis", "path": ".env"})
    assert answer == server.REPO_SENSITIVE_REFUSAL
    assert "API_KEY" not in answer and "FISH" not in answer


@pytest.mark.asyncio
async def test_the_refusal_is_not_simply_everything_failing(ready):
    """The trap this project has fallen into before: a guard that looks alive
    because the fixture was rejected for an unrelated reason. A neighbouring,
    non-secret file must come back."""
    server = ready
    answer = await server.tool_read_file({"project": "jarvis",
                                          "path": ".env.example"})
    assert answer != server.REPO_SENSITIVE_REFUSAL
    assert "MAC_VOICE" in answer, "the documented template should read fine"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../../../etc/passwd", "/etc/passwd",
                                  "~/.ssh/id_rsa", "data/jarvis/tool-token"])
async def test_nothing_outside_him_or_private_inside_him_is_read(ready, path):
    server = ready
    answer = await server.tool_read_file({"project": "jarvis", "path": path})
    assert answer in (server.REPO_SENSITIVE_REFUSAL,
                      server.REPO_OUTSIDE_REFUSAL.format(
                          name=server.JARVIS_SELF_NAME)), answer


def test_his_own_loopback_token_is_refused(ready):
    """Reading his own repo put `data/jarvis/tool-token` in range — the
    bearer token that is the only thing between a local process and every
    acting tool he has. `data/` defaults to a directory inside the repo, so
    this is not hypothetical. Refused by the existing wall, by exact name."""
    import repo_read
    assert repo_read.sensitive_reason(Path("data/jarvis/tool-token"))
    assert repo_read.sensitive_reason(Path("tokenizer.py")) is None, (
        "'token' as a fragment would refuse ordinary code")


# --- and nothing may WRITE to him -----------------------------------------

def test_he_is_not_a_project_anything_can_be_spawned_in(ready):
    """`spawn_run`, `run_command`, `start_build` and `create_project` all
    resolve through `_resolve_project_or_explain`, which must not know about
    him: an unattended `claude -p --dangerously-skip-permissions` editing the
    source it is running on is nobody's idea of a good time."""
    server = ready
    name, path, problem = server._resolve_project_or_explain("jarvis")
    assert problem is not None
    assert name is None and path is None
    assert str(server._jarvis_source_root()) not in problem


def test_his_root_is_not_in_the_project_candidates(ready):
    server = ready
    roots = {str(p) for paths in server._project_candidates().values()
             for p in paths}
    assert str(server._jarvis_source_root()) not in roots
