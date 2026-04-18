"""
Embedded action dispatch — handles [ACTION:*] tags emitted inline by
the LLM's response. Each branch either fires off a background task
(so JARVIS stays conversational) or records something to MC / memory.

Per-connection state (ws, work_session, history, voice_state) and
cross-module adapters (execute_prompt_project, self_work_and_notify,
_execute_browse, etc.) are injected from the voice handler.
"""

import asyncio
import base64
import contextlib
import logging
import os
import re
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any

from actions import _generate_project_name
from formatting import strip_markdown_for_tts
from mc_client import mc_client
from memory import create_note, remember
from notes_access import create_apple_note, read_note
from tts import synthesize_speech

from .action_handlers import execute_browse, execute_open_terminal

log = logging.getLogger("jarvis.embedded_actions")


_TIME_RE_H = re.compile(r"(\d+)\s*h")
_TIME_RE_M = re.compile(r"(\d+)\s*m")
_TIME_RE_S = re.compile(r"(\d+)\s*s")
_TIME_RE_BARE = re.compile(r"(\d+)")


def _parse_timer_seconds(time_str: str) -> int:
    """Parse '5 minutes' / '1h 30m' / bare '5' (as minutes) → seconds."""
    seconds = 0
    if hrs := _TIME_RE_H.search(time_str):
        seconds += int(hrs.group(1)) * 3600
    if mins := _TIME_RE_M.search(time_str):
        seconds += int(mins.group(1)) * 60
    if secs := _TIME_RE_S.search(time_str):
        seconds += int(secs.group(1))
    if not seconds and (bare := _TIME_RE_BARE.search(time_str)):
        seconds = int(bare.group(1)) * 60
    return seconds


async def _send_audio(ws: Any, text: str) -> None:
    """TTS a message and push it over the WebSocket. Silently ignores ws errors."""
    audio = await synthesize_speech(strip_markdown_for_tts(text))
    if not audio or not ws:
        return
    with contextlib.suppress(Exception):
        await ws.send_json({"type": "status", "state": "speaking"})
        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": text})


async def _timer_fire(seconds: int, msg: str, ws: Any) -> None:
    await asyncio.sleep(seconds)
    await _send_audio(ws, msg)


async def _read_and_report(search_term: str, ws: Any) -> None:
    note = await read_note(search_term)
    if note:
        msg = f"Sir, your note '{note['title']}' says: {note['body'][:200]}"
    else:
        msg = f"Couldn't find a note matching '{search_term}', sir."
    await _send_audio(ws, msg)


def is_injected(action: dict, ctx_cache: dict) -> bool:
    """True if the action tag appears in untrusted context (calendar/mail/screen).

    Protects against prompt-injection via scraped email subjects, etc.
    """
    action_tag = f"[ACTION:{action['action'].upper()}]"
    untrusted = [ctx_cache.get("calendar", ""), ctx_cache.get("mail", ""), ctx_cache.get("screen", "")]
    return any(action_tag in ctx for ctx in untrusted if ctx)


def default_response_for(action_type: str, target: str) -> str:
    """Fallback text when the LLM emits only the tag and no reply."""
    if action_type == "prompt_project":
        proj = target.split("|||")[0].strip()
        return f"Connecting to {proj} now, sir."
    if action_type == "build":
        return "On it, sir."
    if action_type == "research":
        return "Looking into that now, sir."
    return "Right away, sir."


async def dispatch(
    action: dict,
    *,
    ws: Any,
    work_session: Any,
    history: list[dict],
    voice_state: dict,
    dispatch_registry: Any,
    execute_prompt_project: Callable[..., Coroutine[Any, Any, None]],
    self_work_and_notify: Callable[..., Coroutine[Any, Any, None]],
    lookup_and_report: Callable[..., Coroutine[Any, Any, None]],
    do_screen_lookup: Callable[[], Awaitable[str]],
) -> None:
    """Route a single [ACTION:*] tag to its handler.

    All handlers are fire-and-forget (asyncio.create_task) except memory
    writes and MC task creates that complete quickly.
    """
    kind = action["action"]
    target = action["target"]

    if kind == "build":
        mc_task = await mc_client.create_task(
            title=_generate_project_name(target),
            description=target,
            importance="important",
            urgency="urgent",
            assigned_to="developer",
        )
        if mc_task:
            log.info(f"MC build task created: {mc_task['id']} — {mc_task['title']}")
            return
        log.warning("MC offline — falling back to direct dispatch")
        name = _generate_project_name(target)
        path = str(Path.home() / "Desktop" / name)
        os.makedirs(path, exist_ok=True)
        Path(path, "CLAUDE.md").write_text(f"# Task\n\n{target}\n\nBuild this completely.\n")
        did = dispatch_registry.register(name, path, target)
        asyncio.create_task(
            execute_prompt_project(
                name, target, work_session, ws, dispatch_id=did, history=history, voice_state=voice_state
            )
        )

    elif kind == "browse":
        asyncio.create_task(execute_browse(target))

    elif kind == "research":
        mc_task = await mc_client.create_task(
            title=f"Research: {target[:80]}",
            description=target,
            importance="important",
            urgency="not-urgent",
            assigned_to="researcher",
        )
        if mc_task:
            log.info(f"MC research task created: {mc_task['id']}")
            return
        log.warning("MC offline — falling back to direct research")
        name = _generate_project_name(target)
        path = str(Path.home() / "Desktop" / name)
        os.makedirs(path, exist_ok=True)
        await work_session.start(path)
        asyncio.create_task(self_work_and_notify(work_session, target, ws))

    elif kind == "open_terminal":
        asyncio.create_task(execute_open_terminal())

    elif kind == "prompt_project":
        if "|||" not in target:
            log.warning(f"PROMPT_PROJECT missing ||| delimiter: {target}")
            return
        proj_name, _, prompt = target.partition("|||")
        asyncio.create_task(
            execute_prompt_project(
                proj_name.strip(),
                prompt.strip(),
                work_session,
                ws,
                history=history,
                voice_state=voice_state,
            )
        )

    elif kind == "add_task":
        parts = target.split("|||")
        if len(parts) < 2:
            return
        priority = parts[0].strip() or "medium"
        title = parts[1].strip()
        desc = parts[2].strip() if len(parts) > 2 else ""
        importance = "important" if priority in ("high", "medium") else "not-important"
        urgency = "urgent" if priority == "high" else "not-urgent"
        await mc_client.create_task(
            title=title, description=desc, importance=importance, urgency=urgency, assigned_to="me"
        )
        log.info(f"MC task created: {title}")

    elif kind == "add_note":
        if "|||" in target:
            topic, _, content = target.partition("|||")
            create_note(content=content.strip(), topic=topic.strip())
        else:
            create_note(content=target)
        log.info("Note created")

    elif kind == "complete_task":
        task_id = target.strip()
        if task_id:
            await mc_client.complete_task(task_id)
            log.info(f"MC task {task_id} completed")

    elif kind == "remember":
        remember(target.strip(), mem_type="fact", importance=7)
        log.info(f"Memory stored: {target[:60]}")

    elif kind == "create_note":
        if "|||" in target:
            title, _, body = target.partition("|||")
            asyncio.create_task(create_apple_note(title.strip(), body.strip()))
            log.info(f"Apple Note created: {title.strip()}")
        else:
            asyncio.create_task(create_apple_note("JARVIS Note", target))

    elif kind == "screen":
        asyncio.create_task(lookup_and_report("screen", do_screen_lookup, ws, history=history, voice_state=voice_state))

    elif kind == "read_note":
        asyncio.create_task(_read_and_report(target.strip(), ws))

    elif kind == "set_timer":
        parts = target.split("|||")
        time_str = parts[0].strip()
        reminder_msg = parts[1].strip() if len(parts) > 1 else "Your timer is up, sir."
        seconds = _parse_timer_seconds(time_str)
        if seconds > 0:
            asyncio.create_task(_timer_fire(seconds, reminder_msg, ws))
            log.info(f"Timer set: {seconds}s — {reminder_msg}")
