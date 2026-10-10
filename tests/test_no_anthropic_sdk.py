"""JARVIS makes no Anthropic API calls, and no longer depends on the SDK.

The README's headline promise is that JARVIS runs entirely on the user's
Claude Code subscription. `requirements.txt` contradicted that by installing
the `anthropic` package, which survived only because `server.py` imported it
at module scope for a dispatch chain the brain + RunExecutor + TOOL_HANDLERS
architecture had already superseded:

    _execute_prompt_project -> _await_and_report -> _report_run_result
    self_work_and_notify
    _build_anthropic_client / anthropic_client

None of it had a non-test caller. This pins the removal: no SDK import, no
SDK dependency, no client, no chain -- and no settings endpoint that offers
to configure an API-key workspace that nothing would ever read.

What deliberately survives: `preflight._check_anthropic_key_leftover_sync`,
which warns the user that a stray ANTHROPIC_* variable in `.env` is a sign
their setup is off. That check reads `os.environ` and imports nothing.
"""
import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))
ROOT = Path(__file__).parent.parent


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)

    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server
    importlib.reload(server)
    run_store.init_db()

    # Redirect .env reads/writes to an isolated file so these tests never
    # touch the real project .env.
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(server, "_env_file_path", lambda: env_file)

    with TestClient(server.app,
                    headers={"Origin": "http://localhost:5173"}) as c:
        yield c, server


# ---------------------------------------------------------------------------
# The dependency and the import
# ---------------------------------------------------------------------------

def test_requirements_does_not_install_the_anthropic_sdk():
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    for line in text.splitlines():
        name = line.split("#")[0].strip().split("=")[0].split(">")[0].split("[")[0]
        assert name.lower() != "anthropic", f"anthropic is back in requirements: {line!r}"


def test_server_does_not_import_the_anthropic_sdk():
    text = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "import anthropic" not in text


def test_no_module_imports_the_anthropic_sdk():
    """Not just server.py -- nothing in the project may pull the SDK in."""
    offenders = []
    for path in sorted(ROOT.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "import anthropic" in text or "from anthropic" in text:
            offenders.append(path.name)
    assert offenders == []


def test_server_module_has_no_anthropic_attribute(client):
    _, server = client
    assert not hasattr(server, "anthropic")


# ---------------------------------------------------------------------------
# The chain the import existed for
# ---------------------------------------------------------------------------

def test_the_anthropic_client_is_gone(client):
    _, server = client
    assert not hasattr(server, "anthropic_client")
    assert not hasattr(server, "_build_anthropic_client")
    assert not hasattr(server, "ANTHROPIC_API_KEY")
    assert not hasattr(server, "ANTHROPIC_WORKSPACE_ID")


def test_the_dead_dispatch_chain_is_gone(client):
    """Superseded by the brain + RunExecutor + TOOL_HANDLERS architecture.
    Only tests called any of it."""
    _, server = client
    for name in ("_execute_prompt_project", "_await_and_report",
                 "_report_run_result", "self_work_and_notify",
                 "_fail_run_if_not_terminal", "_recent_run_for_project",
                 "_start_work_project", "_work_project_send",
                 "_find_project_dir", "_execute_browse",
                 "VOICE_REPORT_WAIT_SEC", "WORK_MODE_WAIT_SEC"):
        assert not hasattr(server, name), f"{name} is back in server.py"


def test_lifespan_no_longer_warns_about_a_missing_api_key():
    """The startup log told every user "ANTHROPIC_API_KEY not set -- LLM
    features disabled", which was both false and the opposite of the
    project's promise."""
    text = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "LLM features disabled" not in text


# ---------------------------------------------------------------------------
# /api/settings -- no key-workspace configuration survives the SDK
# ---------------------------------------------------------------------------

def test_settings_keys_rejects_the_workspace_id(client):
    """Nothing reads ANTHROPIC_WORKSPACE_ID any more, and writing it into
    `.env` would trip preflight's own anthropic_key_leftover warning. The
    endpoint must not offer to create that state."""
    c, server = client
    r = c.post("/api/settings/keys", json={
        "key_name": "ANTHROPIC_WORKSPACE_ID",
        "key_value": "wrkspc-abc",
    })
    assert r.status_code == 400
    assert r.json()["success"] is False
    _, parsed = server._read_env()
    assert "ANTHROPIC_WORKSPACE_ID" not in parsed


def test_settings_keys_rejects_unknown_name(client):
    c, _ = client
    r = c.post("/api/settings/keys", json={
        "key_name": "SOME_RANDOM_KEY",
        "key_value": "whatever",
    })
    assert r.status_code == 400
    assert r.json()["success"] is False


def test_settings_keys_still_accepts_the_keys_jarvis_actually_uses(client):
    c, server = client
    for name, value in (("FISH_API_KEY", "fish-abc"),
                        ("FISH_VOICE_ID", "voice-abc"),
                        ("USER_NAME", "Tony"),
                        ("HONORIFIC", "sir")):
        r = c.post("/api/settings/keys",
                   json={"key_name": name, "key_value": value})
        assert r.status_code == 200, name
        assert r.json()["success"] is True
    _, parsed = server._read_env()
    assert parsed["FISH_API_KEY"] == "fish-abc"
    assert parsed["USER_NAME"] == "Tony"


def test_settings_status_does_not_report_a_workspace_id(client):
    c, _ = client
    r = c.get("/api/settings/status")
    assert r.status_code == 200
    assert "anthropic_workspace_id" not in r.json()["env_keys_set"]
    assert "anthropic" not in r.text.lower()


# ---------------------------------------------------------------------------
# What survives
# ---------------------------------------------------------------------------

def test_preflight_still_warns_about_a_stray_anthropic_variable(monkeypatch):
    """The check is about the user's `.env`, not about the SDK -- it stays,
    and it must not need the package to do its job."""
    import preflight
    text = (ROOT / "preflight.py").read_text(encoding="utf-8")
    assert "import anthropic" not in text and "from anthropic" not in text

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-abc123")
    check = preflight._check_anthropic_key_leftover_sync()
    assert check.status == preflight.STATUS_WARN
    assert "ANTHROPIC_API_KEY" in check.message
