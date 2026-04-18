"""
Action handlers — side-effect functions that execute JARVIS actions.

Owns `recently_built`: a short-lived list tracking builds/research for
"show me what you built" recall. Other modules import it; only handlers
here append to it.
"""

import asyncio
import logging
import os
import time
from pathlib import Path

from macos.actions import _generate_project_name, open_browser, open_terminal
from sanitize import DANGEROUS_FLAG, escape_shell_in_applescript

log = logging.getLogger("jarvis.action_handlers")

recently_built: list[dict] = []  # [{"name": str, "path": str, "time": float}]


async def handle_open_terminal() -> str:
    result = await open_terminal(f"claude{DANGEROUS_FLAG}")
    return result["confirmation"]


async def handle_build(target: str) -> str:
    name = _generate_project_name(target)
    path = str(Path.home() / "Desktop" / name)
    os.makedirs(path, exist_ok=True)

    claude_md = Path(path) / "CLAUDE.md"
    claude_md.write_text(f"# Task\n\n{target}\n\nBuild this completely. If web app, make index.html work standalone.\n")

    prompt_file = Path(path) / ".jarvis_prompt.txt"
    prompt_file.write_text(target)

    script = (
        'tell application "Terminal"\n'
        "    activate\n"
        f'    do script "cd {escape_shell_in_applescript(path)} && cat .jarvis_prompt.txt | claude -p{DANGEROUS_FLAG}"\n'
        "end tell"
    )
    await asyncio.create_subprocess_exec(
        "osascript",
        "-e",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    recently_built.append({"name": name, "path": path, "time": time.time()})
    return f"On it, sir. Claude Code is working in {name}."


async def execute_browse(target: str) -> None:
    """Execute a browse action from an LLM-embedded [ACTION:BROWSE] tag."""
    try:
        if target.startswith("http") or "." in target.split()[0]:
            await open_browser(target)
        else:
            from urllib.parse import quote

            await open_browser(f"https://www.google.com/search?q={quote(target)}")
    except Exception as e:
        log.error(f"Browse execution failed: {e}")


async def execute_open_terminal() -> None:
    """Execute an open-terminal action from an LLM-embedded [ACTION:OPEN_TERMINAL] tag."""
    try:
        await handle_open_terminal()
    except Exception as e:
        log.error(f"Open terminal failed: {e}")


async def handle_show_recent() -> str:
    if not recently_built:
        return "Nothing built recently, sir."
    last = recently_built[-1]
    project_path = Path(last["path"])

    for name in ["report.html", "index.html"]:
        f = project_path / name
        if f.exists():
            await open_browser(f"file://{f}")
            return f"Opened {name} from {last['name']}, sir."

    html_files = list(project_path.glob("*.html"))
    if html_files:
        await open_browser(f"file://{html_files[0]}")
        return f"Opened {html_files[0].name} from {last['name']}, sir."

    script = f'tell application "Finder"\nactivate\nopen POSIX file "{last["path"]}"\nend tell'
    await asyncio.create_subprocess_exec(
        "osascript", "-e", script, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    return f"Opened the {last['name']} folder in Finder, sir."
