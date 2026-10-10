"""The gate is wired into the brain's own launch, or it is decoration.

A gate that is never installed is worse than no gate, because the next
person to read the code believes it is running.
"""

import json

import pytest


def _settings(cmd):
    return json.loads(cmd[cmd.index("--settings") + 1])


@pytest.fixture
def cmd(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import importlib
    import data_paths
    importlib.reload(data_paths)
    import brain
    importlib.reload(brain)
    b = brain.Brain(brain.BrainConfig(home=tmp_path, connections=["linkedin"]))
    return b.command()


def test_a_pretooluse_hook_is_installed(cmd):
    hooks = _settings(cmd).get("hooks", {}).get("PreToolUse")
    assert hooks, f"no PreToolUse hook in --settings: {_settings(cmd)}"


def test_it_matches_other_servers_and_not_jarviss_own(cmd):
    """JARVIS's own tools are gated at /internal/tool already. Gating them
    here deadlocks: the gate's bookkeeping runs through tools of his."""
    import re
    matcher = _settings(cmd)["hooks"]["PreToolUse"][0]["matcher"]
    assert re.match(matcher, "mcp__linkedin__create_post")
    assert not re.match(matcher, "mcp__jarvis__read_file")


def test_the_hook_is_told_where_to_ask_and_how_to_authenticate(cmd):
    """It runs as its own process, so it gets the loopback URL and the path
    to the token file — never the token itself, which would put a secret in
    a process command line."""
    import data_paths
    command = _settings(cmd)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "pretool_hook.py" in command
    assert "/internal/pretool" in command
    assert str(data_paths.tool_token_path()) in command
    token = data_paths.ensure_tool_token()
    assert token and token not in command, "the token itself must not be on a command line"


def test_the_settings_keep_what_was_already_there(cmd):
    """`crossSessionInbound` was the whole of --settings before this."""
    assert _settings(cmd).get("crossSessionInbound") == "accept"


def test_skip_permissions_is_still_passed(cmd):
    """It has to be — a run has no TTY to answer a prompt. The point is that
    it does NOT disable the hook: verified against CLI 2.1.270, a deny still
    stops the call and the user's server never runs the tool."""
    assert "--dangerously-skip-permissions" in cmd
