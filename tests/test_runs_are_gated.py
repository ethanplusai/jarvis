"""A spawned run does not get past the gate the brain is held to.

The PreToolUse gate was installed in `brain.settings()` — on the BRAIN's
argv, and nowhere else. `RunExecutor._command` built
`claude -p ... --dangerously-skip-permissions` with no `--settings`, no
`--strict-mcp-config` and no `--mcp-config`, and `_spawn` handed it
`claude_env.child_env()`, which deliberately preserves HOME and
CLAUDE_CONFIG_DIR.

So a run loaded the USER'S OWN `~/.claude.json` MCP servers, with
permissions skipped and no hook. Verified live against CLI 2.1.270: a
process launched with exactly those flags reported mid-run that its tool
list contained `mcp__linkedin__send_message` and
`mcp__linkedin__connect_with_person`. The servers are still connecting at
init, so they are absent from a quick check and present about a minute
later — invisible to a glance, live for the long unattended build.

The escalation needs nothing unusual: "start the build in that repo" is a
tool the brain may legitimately call with origin=user, and the agent it
spawns can then post to LinkedIn on its own initiative, or because a README
it read told it to. Nothing is staged, nothing is spoken, nothing is
recorded. Everything built today protects one process, and the brain could
spawn past it.

Two independent answers, because one of them is a policy that a future
edit could reverse by accident:

  --strict-mcp-config   a run gets NO MCP servers at all. Verified: with
                        that flag and no --mcp-config, the CLI answers
                        "NONE" when asked what MCP tools it has.
  the PreToolUse hook   so that if a config is ever handed to runs, the
                        calls are gated exactly as the brain's are.
"""

import json

import pytest


@pytest.fixture
def cmd(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import importlib
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import run_executor
    importlib.reload(run_executor)
    run_store.init_db()
    ex = run_executor.RunExecutor(run_store)
    return ex._command("run-1", None)


def test_a_run_gets_no_mcp_servers_of_the_users_own(cmd):
    assert "--strict-mcp-config" in cmd, (
        "a run inherits ~/.claude.json and can reach the user's LinkedIn")
    assert "--mcp-config" not in cmd, (
        "strict with a config would hand the run whatever that config names")


def test_a_run_carries_the_same_pretooluse_gate_as_the_brain(cmd):
    """Defence in depth. The flag above is a policy; a later edit that hands
    runs a config must not silently un-gate them."""
    assert "--settings" in cmd, "no settings at all, so no hook"
    settings = json.loads(cmd[cmd.index("--settings") + 1])
    hooks = settings.get("hooks", {}).get("PreToolUse")
    assert hooks, settings


def test_the_run_and_the_brain_share_one_definition_of_the_hook(cmd, tmp_path):
    """Two copies drift, and the copy that drifts is the one nobody looks
    at. Both are built by the same function."""
    import brain
    import claude_env
    run_hook = json.loads(cmd[cmd.index("--settings") + 1])["hooks"]["PreToolUse"]
    brain_hook = brain.Brain(brain.BrainConfig(home=tmp_path)).settings()["hooks"]["PreToolUse"]
    assert run_hook[0]["matcher"] == brain_hook[0]["matcher"]
    assert run_hook[0]["hooks"][0]["timeout"] == brain_hook[0]["hooks"][0]["timeout"]
    assert hasattr(claude_env, "pretool_hook_settings"), \
        "the hook is spelled out twice instead of once"


def test_skip_permissions_is_still_there(cmd):
    """It has to be — a run has no TTY. That is exactly why the rest of this
    file exists."""
    assert "--dangerously-skip-permissions" in cmd
