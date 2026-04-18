"""
Claude Code dispatch — spawns claude -p subprocesses for research and
project work, reports results back via voice.

Functions that need runtime state (anthropic_client, dispatch_registry,
cached_projects) accept it as arguments so the module stays testable.
"""

import asyncio
import base64
import contextlib
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from action_handlers import recently_built
from actions import _generate_project_name, open_browser
from formatting import strip_markdown_for_tts
from sanitize import DANGEROUS_FLAG_LIST, escape_applescript
from tts import synthesize_speech
from work_mode import WorkSession, session_manager

log = logging.getLogger("jarvis.dispatch")


async def execute_research(target: str, ws: Any = None) -> None:
    """Run claude -p in background, open report, speak when done."""
    try:
        name = _generate_project_name(target)
        path = str(Path.home() / "Desktop" / name)
        os.makedirs(path, exist_ok=True)

        prompt = (
            f"{target}\n\n"
            f"Research this thoroughly. Find REAL data — not made-up examples.\n"
            f"Create a well-designed HTML file called `report.html` in the current directory.\n"
            f"Dark theme, clean typography, organized sections, real links and sources.\n"
            f"The working directory is: {path}"
        )

        log.info(f"Research started via claude -p in {path}")

        process = await asyncio.create_subprocess_exec(
            "claude",
            "-p",
            "--output-format",
            "text",
            *DANGEROUS_FLAG_LIST,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=path,
        )

        stdout, _ = await asyncio.wait_for(
            process.communicate(input=prompt.encode()),
            timeout=300,
        )

        result = stdout.decode().strip()
        log.info(f"Research complete ({len(result)} chars)")

        recently_built.append({"name": name, "path": path, "time": time.time()})

        report = Path(path) / "report.html"
        if not report.exists():
            html_files = list(Path(path).glob("*.html"))
            if html_files:
                report = html_files[0]

        if report.exists():
            await open_browser(f"file://{report}")
            log.info(f"Opened {report.name} in browser")

        if ws:
            with contextlib.suppress(Exception):
                notify_text = "Research is complete, sir. Report is open in your browser."
                audio = await synthesize_speech(notify_text)
                if audio:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": notify_text})
                    await ws.send_json({"type": "status", "state": "idle"})
                    log.info(f"JARVIS: {notify_text}")

    except TimeoutError:
        log.error("Research timed out after 5 minutes")
        if ws:
            with contextlib.suppress(Exception):
                audio = await synthesize_speech("Research timed out, sir. It was taking too long.")
                if audio:
                    await ws.send_json(
                        {"type": "audio", "data": base64.b64encode(audio).decode(), "text": "Research timed out, sir."}
                    )
    except Exception as e:
        log.error(f"Research execution failed: {e}")


async def focus_terminal_window(project_name: str) -> None:
    """Bring a Terminal window for the project to front.

    Uses tmux attach when a session exists, falls back to AppleScript window search.
    """
    session = session_manager.find_session(project_name)
    if session and await session.is_alive():
        await session_manager.attach_in_terminal(session.name)
        return

    escaped = escape_applescript(project_name)
    script = f'''
tell application "Terminal"
    repeat with w in windows
        if name of w contains "{escaped}" then
            set index of w to 1
            activate
            exit repeat
        end if
    end repeat
end tell
'''
    with contextlib.suppress(Exception):
        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5)


def find_project_dir(project_name: str, cached_projects: list[dict]) -> str | None:
    """Find a project directory by name from cached projects or common locations."""
    for p in cached_projects:
        if project_name.lower() in p.get("name", "").lower():
            return p.get("path")
    search_dirs = [
        Path.home() / "Desktop",
        Path.home() / "Documents",
        Path.home() / "IdeaProjects",
        Path.home() / "Projects",
    ]
    for search_dir in search_dirs:
        direct = search_dir / project_name
        if direct.is_dir():
            return str(direct)
        try:
            for d in search_dir.iterdir():
                if d.is_dir() and project_name.lower() in d.name.lower():
                    return str(d)
        except (PermissionError, FileNotFoundError):
            continue
    return None


async def execute_prompt_project(
    project_name: str,
    prompt: str,
    work_session: WorkSession,
    ws: Any,
    *,
    anthropic_client: Any,
    dispatch_registry: Any,
    cached_projects: list[dict],
    dispatch_id: int | None = None,
    history: list[dict] | None = None,
    voice_state: dict | None = None,
) -> None:
    """Dispatch a prompt to Claude Code in a project directory.

    Runs entirely in the background. JARVIS returns to conversation mode
    immediately. When Claude Code finishes, JARVIS interrupts to report.
    """
    try:
        project_dir = find_project_dir(project_name, cached_projects)

        if dispatch_id is None:
            dispatch_id = dispatch_registry.register(project_name, project_dir or "", prompt)

        if not project_dir:
            msg = f"Couldn't find the {project_name} project directory, sir."
            audio = await synthesize_speech(msg)
            if audio and ws:
                with contextlib.suppress(Exception):
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
            return

        dispatch = WorkSession()
        await dispatch.start(project_dir, project_name)

        asyncio.create_task(focus_terminal_window(project_name))

        log.info(f"Dispatching to {project_name} in {project_dir}: {prompt[:80]}")
        dispatch_registry.update_status(dispatch_id, "building")

        full_response = await dispatch.send(prompt)
        await dispatch.stop()

        running_match = re.search(r"RUNNING_AT=(https?://localhost:\d+)", full_response or "")
        if not running_match:
            running_match = re.search(r"https?://localhost:\d+", full_response or "")
        if running_match:
            url = running_match.group(1) if running_match.lastindex else running_match.group(0)
            asyncio.create_task(open_browser(url))
            log.info(f"Auto-opening {url}")
            if dispatch_id:
                dispatch_registry.update_status(
                    dispatch_id, "completed", response=full_response[:2000], summary=f"Running at {url}"
                )

        if not full_response or full_response.startswith("Hit a problem") or full_response.startswith("That's taking"):
            dispatch_registry.update_status(
                dispatch_id, "failed" if full_response else "timeout", response=full_response or ""
            )
            msg = f"Sir, I ran into an issue with {project_name}. {full_response[:150] if full_response else 'No response received.'}"
        else:
            if anthropic_client:
                try:
                    summary = await anthropic_client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=150,
                        system=(
                            "You are JARVIS reporting back on what you found or built in a project. "
                            "Speak in first person — 'I found', 'I built', 'I reviewed'. "
                            "Start with 'Sir, ' to get the user's attention. "
                            "Be specific but concise — highlight the key findings or actions taken. "
                            "If there are multiple items, give the count and top 2-3 briefly. "
                            "End by asking how the user wants to proceed. "
                            "NEVER read out URLs or localhost addresses. NEVER say 'Claude Code'. "
                            "2-3 sentences max. No markdown. Natural spoken voice."
                        ),
                        messages=[
                            {
                                "role": "user",
                                "content": f"Project: {project_name}\nClaude Code reported:\n{full_response[:3000]}",
                            }
                        ],
                    )
                    msg = summary.content[0].text
                except Exception:
                    msg = f"Sir, {project_name} finished. Here's the gist: {full_response[:200]}"
            else:
                msg = f"Sir, {project_name} is done. {full_response[:200]}"

        log.info(f"Dispatch summary for {project_name}: {msg[:100]}")
        if voice_state and time.time() - voice_state["last_user_time"] < 3:
            log.info(f"Skipping dispatch audio for {project_name} — user spoke recently")
        else:
            audio = await synthesize_speech(strip_markdown_for_tts(msg))
            if ws:
                try:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    if audio:
                        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                        log.info(f"Dispatch audio sent for {project_name}")
                    else:
                        await ws.send_json({"type": "text", "text": msg})
                        log.info(f"Dispatch text fallback sent for {project_name}")
                except Exception as e:
                    log.error(f"Dispatch audio send failed: {e}")

        if history is not None:
            history.append({"role": "assistant", "content": f"[Dispatch result for {project_name}]: {msg}"})

        dispatch_registry.update_status(dispatch_id, "completed", response=full_response[:2000], summary=msg[:200])
        log.info(f"Project {project_name} dispatch complete ({len(full_response)} chars)")

    except Exception as e:
        log.error(f"Prompt project failed: {e}", exc_info=True)
        with contextlib.suppress(Exception):
            msg = f"Had trouble connecting to {project_name}, sir."
            audio = await synthesize_speech(msg)
            if audio and ws:
                await ws.send_json({"type": "status", "state": "speaking"})
                await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})


async def self_work_and_notify(session: WorkSession, prompt: str, ws: Any, *, anthropic_client: Any) -> None:
    """Run claude -p in background and notify via voice when done."""
    try:
        full_response = await session.send(prompt)
        log.info(f"Background work complete ({len(full_response)} chars)")

        if anthropic_client and full_response:
            try:
                summary = await anthropic_client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=100,
                    system="You are JARVIS. Summarize what you just completed in 1 sentence. First person — 'I built', 'I set up'. No markdown. Never say 'Claude Code'.",
                    messages=[{"role": "user", "content": f"Claude Code completed:\n{full_response[:2000]}"}],
                )
                msg = summary.content[0].text
            except Exception:
                msg = "Work is complete, sir."

            with contextlib.suppress(Exception):
                audio = await synthesize_speech(msg)
                if audio:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                    await ws.send_json({"type": "status", "state": "idle"})
                    log.info(f"JARVIS: {msg}")
    except Exception as e:
        log.error(f"Background work failed: {e}")
