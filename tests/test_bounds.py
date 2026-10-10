"""Bounds an operator can remove by accident are not bounds.

Two shapes, and both were open.

1. `run_executor._resolve_default_timeout` and `_resolve_idle` say in their
   own docstrings that a run may never be unbounded — "there is deliberately
   no way to ask for no timeout", "a bound that can be set to zero is a bound
   an operator can accidentally remove, and the thing it is protecting is a
   permit the whole pipeline shares". They then checked only `> 0`, so
   `JARVIS_RUN_TIMEOUT_SEC=inf` and `=1e30` were both accepted and the wall
   clock was gone.

2. `server._env_value_problem` is the one validator every HTTP-writable
   setting passes through. It refused a line break, a leading space and a
   wrapping quote — and had no length bound at all, so a 100 KB `USER_NAME`
   round-tripped through `POST /api/settings/preferences` and landed
   unwrapped in the brain's `--append-system-prompt`.

Both are tested as CLASSES: every `_resolve_*` in `run_executor` found from
the AST, and every key in `SETTABLE_ENV_KEYS` — not the two the finding
named.
"""

import ast
import importlib
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

EXECUTOR = ROOT / "run_executor.py"


# --- 1. every bound the executor resolves from the environment -----------

def _resolvers() -> dict:
    """{function: env var it reads} for every `_resolve_*` in run_executor
    that takes a bound off the environment. Found in the source, so one
    added next year is bounded here before it can be removed there."""
    tree = ast.parse(EXECUTOR.read_text(encoding="utf-8"))
    out = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if not node.name.startswith("_resolve_"):
            continue
        # The variable is named either at an `os.getenv` in the body or as
        # a literal handed to a shared helper. Both spellings count: the
        # point is "this function takes a bound off the environment", not
        # how it happens to be written this month.
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                    and sub.value.startswith("JARVIS_")):
                out[node.name] = sub.value
                break
    return out


RESOLVERS = _resolvers()


def test_the_resolvers_are_found_in_the_source():
    assert set(RESOLVERS) == {"_resolve_default_timeout", "_resolve_idle"}, \
        RESOLVERS
    assert set(RESOLVERS.values()) == {"JARVIS_RUN_TIMEOUT_SEC",
                                       "JARVIS_RUN_IDLE_SEC"}, RESOLVERS


@pytest.fixture
def executor(monkeypatch):
    monkeypatch.delenv("JARVIS_RUN_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("JARVIS_RUN_IDLE_SEC", raising=False)
    import run_executor
    importlib.reload(run_executor)
    return run_executor


# Values that are numbers, pass `> 0`, and are not wall clocks.
UNBOUNDED = ["inf", "Infinity", "+inf", "1e30", "1e300", "9" * 40]
NOT_A_NUMBER = ["nan", "NaN"]


@pytest.mark.parametrize("name", sorted(RESOLVERS))
@pytest.mark.parametrize("raw", UNBOUNDED)
def test_no_environment_value_can_remove_the_wall_clock(executor, monkeypatch,
                                                        name, raw):
    """`> 0` is not "is a wall clock". `inf` passes it, and so does 1e30 —
    thirty-one orders of magnitude past the heat death of the run."""
    monkeypatch.setenv(RESOLVERS[name], raw)
    importlib.reload(executor)
    value = getattr(executor, name)(None)
    assert math.isfinite(value), (name, raw, value)
    assert 0 < value <= executor.MAX_BOUND_SEC, (name, raw, value)


@pytest.mark.parametrize("name", sorted(RESOLVERS))
@pytest.mark.parametrize("raw", NOT_A_NUMBER)
def test_a_non_number_falls_back_to_the_module_default(executor, monkeypatch,
                                                       name, raw):
    monkeypatch.setenv(RESOLVERS[name], raw)
    importlib.reload(executor)
    value = getattr(executor, name)(None)
    assert math.isfinite(value) and value > 0, (name, raw, value)


@pytest.mark.parametrize("name", sorted(RESOLVERS))
@pytest.mark.parametrize("explicit", [float("inf"), 1e30, float("nan")])
def test_no_caller_can_remove_it_either(executor, name, explicit):
    """A caller's own `timeout_sec` "still wins" — but not past the wall.
    `spawn_run` takes this argument from the brain."""
    value = getattr(executor, name)(explicit)
    assert math.isfinite(value), (name, explicit, value)
    assert 0 < value <= executor.MAX_BOUND_SEC, (name, explicit, value)


@pytest.mark.parametrize("name", sorted(RESOLVERS))
def test_an_ordinary_override_is_still_honoured(executor, monkeypatch, name):
    """A wall that ignores the operator is not a fix."""
    monkeypatch.setenv(RESOLVERS[name], "120")
    importlib.reload(executor)
    assert getattr(executor, name)(None) == 120.0
    assert getattr(executor, name)(90.0) == 90.0


def test_the_maximum_is_a_working_day_or_less(executor):
    """The number itself is a judgement and belongs in one place: long
    enough that no real `start_build` hits it, short enough that a wedged
    run is reclaimed the same day."""
    assert executor.MAX_BOUND_SEC <= 24 * 3600
    assert executor.MAX_BOUND_SEC >= executor._DEFAULT_TIMEOUT_SEC


# --- 2. every setting an HTTP route can write ----------------------------

@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    monkeypatch.setenv("JARVIS_ENV_FILE", str(tmp_path / ".env"))
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server as server_module
    importlib.reload(server_module)
    run_store.init_db()
    return server_module


def test_every_settable_key_is_bounded(server):
    """The class, from `SETTABLE_ENV_KEYS` itself. A key added next year is
    bounded the day it is added, or this fails."""
    keys = sorted(server.SETTABLE_ENV_KEYS)
    assert len(keys) >= 4, keys
    for key in keys:
        problem = server._env_value_problem(key, "x" * 100_000)
        assert problem, f"{key} accepts a hundred thousand characters"
        assert "long" in problem.lower(), (key, problem)


def test_a_name_is_bounded_more_tightly_than_a_key(server):
    """A Fish Audio key is sixty-odd characters of opaque token. A NAME is a
    name — and it is the one that is spliced into the brain's system prompt
    (`brain.launch_prompt`: "The user's name is {…}") with nothing around
    it, so it is the one that must not be a paragraph."""
    for key in ("USER_NAME", "HONORIFIC"):
        assert key in server.ENV_NAME_KEYS, key
        assert server._env_value_problem(key, "x" * 200), key
        assert server._env_value_problem(key, "Tony") is None, key
        assert server._env_value_problem(key, "Étienne d'Arcy-Smith") is None


def test_a_control_character_is_refused(server):
    """`splitlines()` catches the ten separators. An ESC or a BEL is neither
    a separator nor printable, and it round-tripped into the system prompt
    and into whatever renders it."""
    for key in sorted(server.SETTABLE_ENV_KEYS):
        for bad in ("\x1b[2J", "a\x07b", "a\x00b", "a\x7fb"):
            assert server._env_value_problem(key, bad), (key, repr(bad))


def test_the_ordinary_settings_still_save(server):
    for key, value in (("USER_NAME", "Tony"), ("HONORIFIC", "sir"),
                       ("FISH_VOICE_ID", "b545c585f4d491a"),
                       ("FISH_API_KEY", "x" * 64)):
        assert server._env_value_problem(key, value) is None, key
        server._write_env_key(key, value)
    _, parsed = server._read_env()
    assert parsed["USER_NAME"] == "Tony"
    assert parsed["FISH_API_KEY"] == "x" * 64


def test_the_route_refuses_rather_than_saving_half(server):
    """`POST /api/settings/preferences` is the door the finding came
    through. It must answer 400 and write nothing."""
    from fastapi.testclient import TestClient
    with TestClient(server.app) as client:
        r = client.post("/api/settings/preferences",
                        json={"user_name": "x" * 100_000, "honorific": "sir"},
                        headers={"Origin": "http://localhost:5173"})
        assert r.status_code == 400, r.text
        _, parsed = server._read_env()
        assert not parsed.get("USER_NAME"), parsed.get("USER_NAME")
        assert not parsed.get("HONORIFIC"), parsed.get("HONORIFIC")
