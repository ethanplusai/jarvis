from pathlib import Path

import secondary_agent


def test_secondary_agent_profile_is_centralized():
    assert secondary_agent.SECONDARY_AGENT is secondary_agent.CODEX_GPT_5_4
    assert secondary_agent.SECONDARY_AGENT.model == "gpt-5.4"


def test_build_secondary_agent_exec_command_for_codex(monkeypatch):
    monkeypatch.setattr(secondary_agent, "secondary_agent_binary", lambda: "/usr/local/bin/codex")

    cmd = secondary_agent.build_secondary_agent_exec_command(
        working_dir=Path("/tmp/example"),
        continue_session=False,
        output_file=Path("/tmp/example/out.txt"),
    )

    assert cmd[:2] == ["/usr/local/bin/codex", "exec"]
    assert "--full-auto" in cmd
    assert "--skip-git-repo-check" in cmd
    assert "-m" in cmd
    assert secondary_agent.SECONDARY_AGENT.model in cmd
    assert f'model_reasoning_effort="{secondary_agent.SECONDARY_AGENT.reasoning_effort}"' in cmd
    assert str(Path("/tmp/example/out.txt")) in cmd
    assert cmd[-1] == "-"


def test_build_secondary_agent_resume_command_for_codex(monkeypatch):
    monkeypatch.setattr(secondary_agent, "secondary_agent_binary", lambda: "/usr/local/bin/codex")

    cmd = secondary_agent.build_secondary_agent_exec_command(
        working_dir=Path("/tmp/example"),
        continue_session=True,
        output_file=None,
    )

    assert cmd[:4] == ["/usr/local/bin/codex", "exec", "resume", "--last"]
    assert "--full-auto" in cmd
    assert secondary_agent.SECONDARY_AGENT.model in cmd
    assert cmd[-1] == "-"


def test_build_secondary_agent_interactive_command_normalizes_prompt(monkeypatch):
    monkeypatch.setattr(secondary_agent, "secondary_agent_binary", lambda: "/usr/local/bin/codex")

    command = secondary_agent.build_secondary_agent_interactive_command(
        "/tmp/example",
        prompt="Build this\n\ncarefully",
    )

    assert "cd /tmp/example &&" in command
    assert "Build this carefully" in command
    assert "\n" not in command
