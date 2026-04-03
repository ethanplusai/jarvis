"""
JARVIS secondary coding agent runtime.

This module centralizes the CLI + model used for the repo's heavier
non-Haiku work. To switch the work agent later, change the
`SECONDARY_AGENT = ...` line below.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("jarvis.secondary_agent")


@dataclass(frozen=True)
class SecondaryAgentProfile:
    key: str
    display_name: str
    cli_name: str
    model: str
    reasoning_effort: str | None = None
    supports_output_file: bool = False


CODEX_GPT_5_4 = SecondaryAgentProfile(
    key="codex-gpt-5.4",
    display_name="Codex GPT-5.4",
    cli_name="codex",
    model="gpt-5.4",
    reasoning_effort="xhigh",
    supports_output_file=True,
)

CLAUDE_SONNET = SecondaryAgentProfile(
    key="claude-sonnet",
    display_name="Claude Sonnet",
    cli_name="claude",
    model="sonnet",
    reasoning_effort="high",
    supports_output_file=False,
)

# Change this one line to switch the repo-wide secondary agent.
SECONDARY_AGENT = CODEX_GPT_5_4


@dataclass
class SecondaryAgentRunResult:
    returncode: int
    message: str
    stdout: str
    stderr: str


def secondary_agent_installed() -> bool:
    return shutil.which(SECONDARY_AGENT.cli_name) is not None


def secondary_agent_binary() -> str:
    path = shutil.which(SECONDARY_AGENT.cli_name)
    if not path:
        raise FileNotFoundError(f"{SECONDARY_AGENT.cli_name} CLI not found on this system.")
    return path


def build_secondary_agent_exec_command(
    *,
    working_dir: str | Path,
    continue_session: bool = False,
    output_file: str | Path | None = None,
) -> list[str]:
    """Build a non-interactive command for the active secondary agent."""
    binary = secondary_agent_binary()

    if SECONDARY_AGENT.cli_name == "codex":
        if continue_session:
            cmd = [
                binary,
                "exec",
                "resume",
                "--last",
                "--full-auto",
                "--skip-git-repo-check",
                "-m",
                SECONDARY_AGENT.model,
            ]
        else:
            cmd = [
                binary,
                "exec",
                "--full-auto",
                "--skip-git-repo-check",
                "-C",
                str(working_dir),
                "-m",
                SECONDARY_AGENT.model,
            ]

        if SECONDARY_AGENT.reasoning_effort:
            cmd.extend(["-c", f'model_reasoning_effort="{SECONDARY_AGENT.reasoning_effort}"'])
        if output_file:
            cmd.extend(["-o", str(output_file)])
        cmd.append("-")
        return cmd

    if SECONDARY_AGENT.cli_name == "claude":
        cmd = [
            binary,
            "-p",
            "--output-format",
            "text",
            "--dangerously-skip-permissions",
            "--model",
            SECONDARY_AGENT.model,
        ]
        if SECONDARY_AGENT.reasoning_effort:
            cmd.extend(["--effort", SECONDARY_AGENT.reasoning_effort])
        if continue_session:
            cmd.append("--continue")
        return cmd

    raise ValueError(f"Unsupported secondary agent CLI: {SECONDARY_AGENT.cli_name}")


def build_secondary_agent_interactive_command(
    working_dir: str | Path | None = None,
    prompt: str | None = None,
) -> str:
    """Build the shell command used when opening the agent visibly in Terminal."""
    binary = secondary_agent_binary()

    if SECONDARY_AGENT.cli_name == "codex":
        parts = [binary, "--full-auto", "-m", SECONDARY_AGENT.model]
        if SECONDARY_AGENT.reasoning_effort:
            parts.extend(["-c", f'model_reasoning_effort="{SECONDARY_AGENT.reasoning_effort}"'])
    elif SECONDARY_AGENT.cli_name == "claude":
        parts = [binary, "--dangerously-skip-permissions", "--model", SECONDARY_AGENT.model]
        if SECONDARY_AGENT.reasoning_effort:
            parts.extend(["--effort", SECONDARY_AGENT.reasoning_effort])
    else:
        raise ValueError(f"Unsupported secondary agent CLI: {SECONDARY_AGENT.cli_name}")

    if prompt:
        parts.append(" ".join(prompt.split()))

    command = " ".join(shlex.quote(part) for part in parts)
    if working_dir:
        return f"cd {shlex.quote(str(working_dir))} && {command}"
    return command


def build_secondary_agent_batch_shell_command(
    *,
    working_dir: str | Path,
    prompt_file: str | Path,
    output_file: str | Path,
) -> str:
    """Build a shell command that runs the active agent from a prompt file."""
    prompt_name = Path(prompt_file).name
    output_name = Path(output_file).name

    if SECONDARY_AGENT.supports_output_file:
        cmd = build_secondary_agent_exec_command(
            working_dir=".",
            continue_session=False,
            output_file=output_name,
        )
        agent_cmd = " ".join(shlex.quote(part) for part in cmd)
        return (
            f"cd {shlex.quote(str(working_dir))} && "
            f"cat {shlex.quote(prompt_name)} | {agent_cmd}; "
            f"printf '\\n--- JARVIS TASK COMPLETE ---\\n' >> {shlex.quote(output_name)}"
        )

    cmd = build_secondary_agent_exec_command(
        working_dir=".",
        continue_session=False,
        output_file=None,
    )
    agent_cmd = " ".join(shlex.quote(part) for part in cmd)
    return (
        f"cd {shlex.quote(str(working_dir))} && "
        f"cat {shlex.quote(prompt_name)} | {agent_cmd} | tee {shlex.quote(output_name)}; "
        f"printf '\\n--- JARVIS TASK COMPLETE ---\\n' >> {shlex.quote(output_name)}"
    )


async def run_secondary_agent_prompt(
    *,
    prompt: str,
    working_dir: str | Path,
    continue_session: bool = False,
    timeout: float = 300.0,
) -> SecondaryAgentRunResult:
    """Run the active secondary agent and return the final text response."""
    working_dir = str(working_dir)
    output_file: Path | None = None

    if SECONDARY_AGENT.supports_output_file:
        output_file = Path(working_dir) / f".jarvis_secondary_output_{uuid.uuid4().hex}.txt"
        output_file.unlink(missing_ok=True)

    cmd = build_secondary_agent_exec_command(
        working_dir=working_dir,
        continue_session=continue_session,
        output_file=output_file,
    )

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=working_dir,
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(input=prompt.encode()),
            timeout=timeout,
        )
        stdout = stdout_bytes.decode().strip()
        stderr = stderr_bytes.decode().strip()

        message = stdout
        if output_file and output_file.exists():
            file_text = output_file.read_text().strip()
            if file_text:
                message = file_text

        log.info(
            "Secondary agent run complete via %s (%s chars)",
            SECONDARY_AGENT.display_name,
            len(message),
        )

        return SecondaryAgentRunResult(
            returncode=process.returncode,
            message=message,
            stdout=stdout,
            stderr=stderr,
        )
    finally:
        try:
            if output_file:
                output_file.unlink(missing_ok=True)
        except Exception:
            pass
