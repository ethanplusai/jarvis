"""scripts/probe_child_env.py judges a real CLI; these judge the judge.

The probe itself needs a real `claude` and is not run here (see CLAUDE.md:
the suite never invokes one). What it concludes from a run is pure, and a
comparison that quietly stopped seeing a difference would pass every CLI
upgrade.
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _probe():
    spec = importlib.util.spec_from_file_location("probe_child_env",
                                                  ROOT / "scripts" / "probe_child_env.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("probe_child_env", module)
    spec.loader.exec_module(module)
    return module


def _run(p, **kw):
    fields = dict(ended=True, effort="low", user_agents=["claude-cli/2.1.270 (external, sdk-cli)"],
                  requests=3, exports=[], mcp_spread=0.0, early_call=False)
    fields.update(kw)
    return p.Run(**fields)


def test_an_identical_run_is_no_difference():
    p = _probe()
    assert p.differences(_run(p), _run(p)) == []


def test_each_kind_of_difference_is_seen():
    p = _probe()
    base = _run(p)
    assert p.differences(base, _run(p, effort="high")) == ["effort low -> high"]
    assert "User-Agent" in p.differences(base, _run(p, user_agents=["claude-cli/x (agent-sdk/1)"]))[0]
    assert p.differences(base, _run(p, ended=False, requests=12)) == [
        "turn never finished", "model requests 3 -> 12"]
    assert p.differences(base, _run(p, exports=["/v1/logs"])) == ["exports to /v1/logs"]
    assert p.differences(_run(p, mcp_spread=3.1), _run(p, mcp_spread=0.2)) == [
        "MCP servers started over 3.1s -> 0.2s"]
    assert p.differences(base, _run(p, early_call=True)) == ["first model call before MCP was ready"]


def test_one_side_call_more_or_less_is_noise():
    """The CLI's own Haiku side calls vary by one between identical runs."""
    p = _probe()
    assert p.differences(_run(p), _run(p, requests=4)) == []


def test_the_probe_judges_an_older_rule_that_has_no_is_scrubbed(tmp_path):
    """--rule takes `git show <rev>:claude_env.py`; bd5c2ed's had only
    child_env."""
    p = _probe()
    old = tmp_path / "old_claude_env.py"
    old.write_text('def child_env(base):\n'
                   '    return {k: v for k, v in base.items() if not k.startswith("CLAUDE_CODE_")}\n',
                   encoding="utf-8")
    rule = p._load_rule(str(old))
    assert p._scrubbed(rule, "CLAUDE_CODE_EFFORT_LEVEL") is True
    assert p._scrubbed(rule, "API_TIMEOUT_MS") is False


def test_every_variable_the_probe_measures_is_scrubbed_by_the_current_rule():
    """If one is not, the probe's own verdict would be LEAK for it."""
    p = _probe()
    import claude_env
    for s in p.SCENARIOS:
        for name in s.env:
            assert claude_env.is_scrubbed(name), (s.label, name)
