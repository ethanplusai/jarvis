"""
JARVIS Server — Voice AI + Development Orchestration

Handles:
1. WebSocket voice interface (browser audio <-> LLM <-> TTS)
2. Claude Code task manager (spawn/manage claude -p subprocesses)
3. Project awareness (scan Desktop for git repos)
4. REST API for task management
"""

import asyncio
import base64
import json
import logging
import os
import platform
import shutil
import sys
import time
from pathlib import Path

# Load .env file if present
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import anthropic
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from actions import execute_action, monitor_build, open_terminal, open_browser, open_claude_in_project, _generate_project_name, prompt_existing_terminal, applescript_escape
from work_mode import WorkSession, is_casual_question
from screen import get_active_windows, take_screenshot, describe_screen, format_windows_for_context
from calendar_access import get_todays_events, get_upcoming_events, get_next_event, format_events_for_context, format_schedule_summary, refresh_cache as refresh_calendar_cache
from mail_access import get_unread_count, get_unread_messages, get_recent_messages, search_mail, read_message, format_unread_summary, format_messages_for_context, format_messages_for_voice
from memory import (
    remember, recall, get_open_tasks, create_task, complete_task, search_tasks,
    create_note, search_notes, get_tasks_for_date, build_memory_context,
    format_tasks_for_voice, extract_memories, get_important_memories,
    get_recent_memories, delete_memory, get_all_memories, memory_stats,
)
from notes_access import get_recent_notes, read_note, search_notes_apple, create_apple_note
from dispatch_registry import DispatchRegistry
from planner import TaskPlanner, detect_planning_mode, BYPASS_PHRASES
from integrations import provider_env_keys, providers_for_status, skills_for_status, is_configured
from providers import config as provider_config, llm as provider_llm, tts as provider_tts
import skills as skills_system
import onboarding as onboarding_system
import mcp_registry
import mcp_client
import action_log
import system_control

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("jarvis")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
FISH_API_KEY = os.getenv("FISH_API_KEY", "")
FISH_VOICE_ID = os.getenv("FISH_VOICE_ID", "612b878b113047d9a770c069c8b4fdfe")  # JARVIS (MCU)
FISH_API_URL = "https://api.fish.audio/v1/tts"
USER_NAME = os.getenv("USER_NAME", "sir")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def _set_user_name(value: str):
    """Update the in-process user name (used after onboarding learns it)."""
    global USER_NAME
    USER_NAME = value
_SKIP_PERMISSIONS = os.getenv("JARVIS_SKIP_PERMISSIONS", "true").lower() not in ("0", "false", "no")

DESKTOP_PATH = Path.home() / "Desktop"

JARVIS_SYSTEM_PROMPT = """\
You are JARVIS — Just A Rather Very Intelligent System. You serve as {user_name}'s AI assistant, modeled precisely after Tony Stark's AI from the MCU films.

VOICE & PERSONALITY:
- British butler elegance with understated dry wit
- Address {user_name} as "sir" naturally — not every sentence, but regularly
- Never say "How can I help you?" or "Is there anything else?" — just act
- Deliver bad news calmly, like reporting weather: "We have a slight problem, sir."
- Your humor is observational, never jokes: state facts and let implications land
- Be funny in a premium way: quick dry asides, tiny cinematic confidence, never slapstick
- Sound good in audio: short clauses, clean punctuation, no long nested sentences
- Economy of language — say more with less. No filler, no corporate-speak
- When things go wrong, get CALMER, not more alarmed

TIME & WEATHER AWARENESS:
- Current time: {current_time}
- Greet accordingly: "Good morning, sir" / "Good evening, sir"
- {weather_info}

CONVERSATION STYLE:
- "Will do, sir." — acknowledging tasks
- "For you, sir, always." — when asked for something significant
- "As always, sir, a great pleasure watching you work." — dry wit
- "I've taken the liberty of..." — proactive actions
- Lead status reports with data: numbers first, then context
- When you don't know something: "I'm afraid I don't have that information, sir" not "I don't know"

SELF-AWARENESS:
You ARE the JARVIS project at {project_dir} on {user_name}'s computer. Your code is Python (FastAPI server, WebSocket voice, Fish Audio TTS, Anthropic API). You were built by {user_name}. If asked about yourself, your code, how you work, or your line count — use [ACTION:PROMPT_PROJECT] to check the jarvis project. You have full access to your own source code.

YOUR CAPABILITIES (these are REAL and ACTIVE — you CAN do all of these RIGHT NOW):
- You CAN open Terminal.app via AppleScript
- You CAN open Google Chrome and browse any URL or search query
- You CAN spawn Claude Code in a Terminal window for coding tasks
- You CAN create project folders on the Desktop
- You CAN check Desktop projects and their git status
- You CAN plan complex tasks by asking smart questions before executing
- You CAN see what's on {user_name}'s screen — open windows, active apps, and screenshot vision
- You CAN read {user_name}'s calendar — today's events, upcoming meetings, schedule overview
- You CAN read {user_name}'s email (READ-ONLY) — unread count, recent messages, search by sender/subject. You CANNOT send, delete, or modify emails.
- You CAN read Apple Notes and create NEW notes — but you CANNOT edit or delete existing notes
- You CAN manage tasks — create, complete, and list to-do items with priorities and due dates
- You CAN help plan {user_name}'s day — combine calendar events, tasks, and priorities into an organized plan
- You CAN remember facts about {user_name} — preferences, decisions, goals. Use [ACTION:REMEMBER] to store important info.
- You HAVE a library of installable skills for small-business work (sales, marketing, finance, HR, support, ops, and more). Active skills appear under ACTIVE SKILLS. When a skill is marked executable, use [ACTION:RUN_SKILL] slug ||| {{json params}} to actually produce the artifact (e.g. an invoice). Suggest enabling a relevant skill when one would help.
- You CAN connect to external tools (email, docs, CRM, database, design) over MCP. Connected tools appear under CONNECTED TOOLS. If a needed tool isn't connected, suggest connecting it in Settings.

DAY PLANNING:
When {user_name} asks to plan his day or schedule, DO NOT dispatch to a project. Instead:
1. Look at the calendar context and tasks already in your system prompt
2. Ask what his priorities are
3. Help organize by suggesting time blocks and task order
4. Use [ACTION:ADD_TASK] to create tasks he agrees to
5. Use [ACTION:ADD_NOTE] to save the plan as a note
Keep the planning conversational — don't try to do everything in one response.

BUILD PLANNING:
When {user_name} wants to BUILD something new:
- Do NOT immediately dispatch [ACTION:BUILD]. Ask 1-2 quick questions FIRST to nail down specifics.
- Good questions: "What should this look like?" / "Any specific features?" / "Which framework?"
- If he says "just build it" or "figure it out" — skip questions, use React + Tailwind as defaults.
- Once you have enough info, confirm the plan in ONE sentence and THEN dispatch [ACTION:BUILD] with a detailed description.
- The DISPATCHES section shows what you're currently building and what finished recently.
- When asked "where are we at" or "status" — check DISPATCHES, don't re-dispatch.
- NEVER hallucinate progress. If the build is still running, say "Still working on it, sir" — don't make up details about what's happening.
- NEVER guess localhost ports. Check the DISPATCHES section for the actual URL. If a dispatch says "Running at http://localhost:5174" — use THAT URL, not a guess.
- When asked to "pull it up" or "show me" — use [ACTION:BROWSE] with the URL from DISPATCHES. Do NOT dispatch to the project again just to find the URL.
IMPORTANT: Actions like opening Terminal, Chrome, or building projects are handled AUTOMATICALLY by your system — you do NOT need to describe doing them. If the user asks you to build something or search something, your system will handle the execution separately. In your response, just TALK — have a conversation. Don't say "I'll build that now" or "Claude Code is working on..." unless your system has actually triggered the action.
If the user asks you to do something you genuinely can't do, say "I'm afraid that's beyond my current reach, sir." Don't fake executing actions.

YOUR INTERFACE:
The user interacts with you through a web browser showing a particle orb visualization that reacts to your voice. The interface has these controls:
- **Three-dot menu** (top right): opens a right-side drawer with Settings, Control Center brief, Restart Server, and Fix Yourself options.
- **Control Center**: the right HUD contains customizable widgets for JARVIS text summaries, important information, news, weather, time, stock/market snapshots, stats, activity, and action confirmations. Users can hide/pin widgets with Tune. You can send concise cards there with [ACTION:CONTROL_CENTER] title ||| body ||| category.
- **Settings panel**: slides from the right. Users can choose the active model in Engine, tune speech capture language, record local voice reference samples in Voice Capture Lab, group API keys into required vs optional connectors, test connections, set their name/preferences, and see system status. Keys are saved to the .env file.
- **Mute button**: Toggles your listening on/off. When muted, you can't hear the user. They click it again to unmute.
- **Restart Server**: Restarts your backend process. Useful if something seems stuck.
- **Fix Yourself**: Opens Claude Code in your own project directory so you can debug and fix issues in your own code.
- **The orb**: The glowing particle visualization in the center. It reacts to your voice when speaking, pulses when listening, and swirls when thinking.

If asked about any of these, explain them briefly and naturally. If the user is having trouble, suggest the relevant control: "Try the settings panel — the gear icon in the top right." or "The mute button may be active, sir."

SPEECH-TO-TEXT CORRECTIONS (the user speaks, speech recognition may mishear):
- "Cloud code" or "cloud" = "Claude Code" or "Claude"
- "Travis" = "JARVIS"
- "clock code" = "Claude Code"

RESPONSE LENGTH — THIS IS CRITICAL:
ONE sentence is ideal. TWO is the maximum for the spoken part. Never three.
No markdown, no bullet points, no code blocks in voice responses.
Action tags at the end do NOT count toward your sentence limit.

BANNED PHRASES — NEVER USE THESE:
- "Absolutely" / "Absolutely right"
- "Great question"
- "I'd be happy to"
- "Of course"
- "How can I help"
- "Is there anything else"
- "I apologize"
- "I should clarify"
- "I cannot" (for things listed in YOUR CAPABILITIES)
- "I don't have access to" (instead: "I'm afraid that's beyond my current reach, sir")
- "As an AI" (never break character)
- "Let me know if" / "Feel free to"
- Any sentence starting with "I"

INSTEAD SAY:
- "Will do, sir."
- "Right away, sir."
- "Understood."
- "Consider it done."
- "Done, sir."
- "Terminal is open."
- "Pulled that up in Chrome."

ACTION SYSTEM:
When you decide the user needs something DONE (not just discussed), include an action tag in your response:
- [ACTION:SCREEN] — capture and describe what's visible on the user's screen. Use when user says "look at my screen", "what's running", "what do you see", etc. Do NOT use PROMPT_PROJECT for screen requests.
- [ACTION:BUILD] description — when user wants a project built. Claude Code does the work.
- [ACTION:BROWSE] url or search query — when user wants to see a webpage or search result in Chrome
- [ACTION:RESEARCH] detailed research brief — when user wants real research with real data. Claude Code will browse the web, find real listings/data, and create a report document. Give it a detailed brief of what to find.
- [ACTION:OPEN_TERMINAL] — when user just wants a fresh Claude Code terminal with no specific project
CRITICAL: When the user asks about their SCREEN, what's RUNNING, or what they're LOOKING AT — ALWAYS use [ACTION:SCREEN] or let the fast action system handle it. NEVER use [ACTION:PROMPT_PROJECT] for screen requests. PROMPT_PROJECT is ONLY for working on code projects.

- [ACTION:PROMPT_PROJECT] project_name ||| prompt — THIS IS YOUR MOST POWERFUL ACTION. Use it whenever the user wants to work on, jump into, resume, check on, or interact with ANY existing project. You connect directly to Claude Code in that project and can read its response. Craft a clear prompt based on what the user wants. Examples:
  "jump into client engine" → [ACTION:PROMPT_PROJECT] The Client Engine ||| What is the current state of this project? Summarize what was being worked on most recently.
  "check for improvements on my-app" → [ACTION:PROMPT_PROJECT] my-app ||| Review the project and identify improvements we should make.
  "resume where we left off on harvey" → [ACTION:PROMPT_PROJECT] harvey ||| Summarize what was being worked on most recently and what we should focus on next.
- [ACTION:ADD_TASK] priority ||| title ||| description ||| due_date — create a task. Priority: high/medium/low. Due date: YYYY-MM-DD or empty.
  "remind me to call the client tomorrow" → [ACTION:ADD_TASK] medium ||| Call the client ||| Follow up on proposal ||| 2026-03-20
- [ACTION:ADD_NOTE] topic ||| content — save a note for future reference.
  "note that the API key expires in April" → [ACTION:ADD_NOTE] general ||| API key expires in April, need to renew before then
- [ACTION:COMPLETE_TASK] task_id — mark a task as done.
- [ACTION:REMEMBER] content — store an important fact about the user for future context.
  "I prefer React over Vue" → [ACTION:REMEMBER] User prefers React over Vue for frontend projects
- [ACTION:CREATE_NOTE] title ||| body — create a new Apple Note. For saving plans, ideas, lists.
  "save that as a note" → [ACTION:CREATE_NOTE] Day Plan March 19 ||| Morning: client calls. Afternoon: TikTok dashboard. Evening: JARVIS improvements.
- [ACTION:READ_NOTE] title search — read an existing Apple Note by title keyword.
- [ACTION:MCP_CALL] server_id ||| tool_name ||| {json args} — call a connected MCP tool. Read-only calls execute immediately; outbound/write calls are queued for confirmation and logged before anything is sent.
- [ACTION:CONTROL_CENTER] title ||| body ||| category — add or update a concise card in the user's Control Center. Use this for important summaries, news briefs, weather, time, statistics, market/giełda snapshots, reminders, and anything the user asks to see in widgets. Category examples: jarvis, news, weather, markets, stats, alert.

DESKTOP CONTROL (works on macOS, Windows, and Linux; unsupported combos report gracefully):
- [ACTION:OPEN_APP] app name — launch a desktop application. "open Spotify" → [ACTION:OPEN_APP] Spotify
- [ACTION:OPEN_PATH] /path/to/file-or-folder — open a file or folder in the system file manager.
- [ACTION:SET_VOLUME] 0-100 — set the system output volume. "volume to forty percent" → [ACTION:SET_VOLUME] 40
- [ACTION:MEDIA] play_pause|next|previous — control music/media playback.
- [ACTION:LOCK_SCREEN] — lock the computer when the user asks to lock up or step away.
- [ACTION:CLIPBOARD] text — copy the given text to the user's clipboard.
- [ACTION:SCREENSHOT] — capture the screen to a file the user can open later.

You use Claude Code as your tool to build, research, and write code — but YOU are the one doing the work. Never say "Claude Code did X" or "Claude Code is asking" — say "I built X", "I'm checking on that", "I found X". You ARE the intelligence. Claude Code is just your hands.

IMPORTANT: When the user says "jump into X", "work on X", "check on X", "resume X", "go back to X" — ALWAYS use [ACTION:PROMPT_PROJECT]. You have the ability to connect to any project and work on it directly. DO NOT say you can't see terminal history or don't have access — you DO.

Place the tag at the END of your spoken response. Example:
"Right away, sir — connecting to The Client Engine now. [ACTION:PROMPT_PROJECT] The Client Engine ||| Review the current state and what was being worked on. What should we focus on next?"

IMPORTANT:
- Do NOT use action tags for casual conversation
- Do NOT use action tags if the user is still explaining (ask questions first)
- Do NOT use [ACTION:BROWSE] just because someone mentions a URL in conversation
- When in doubt, just TALK — you can always act later

SCREEN AWARENESS:
{screen_context}

SCHEDULE:
{calendar_context}

EMAIL:
{mail_context}

ACTIVE TASKS:
{active_tasks}

DISPATCHES:
If the DISPATCHES section shows a recent completed result for a project, DO NOT dispatch again. Use the existing result. Only re-dispatch if the user explicitly asks for a FRESH review or NEW information.
{dispatch_context}

KNOWN PROJECTS:
{known_projects}
"""


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------
# Location is resolved from (in order): WEATHER_LATITUDE + WEATHER_LONGITUDE
# env vars, a cached IP-geolocation lookup, or a fresh ipwho.is lookup.
# Temperature unit defaults to Fahrenheit; override with WEATHER_UNIT=celsius.

_cached_weather: Optional[str] = None
_weather_fetched: bool = False
_cached_weather_location: Optional[dict] = None
_weather_location_fetched_at: float = 0.0
_WEATHER_LOCATION_TTL_SECONDS = 60 * 15


def _format_location_label(city: str, region: str, country: str) -> str:
    parts = [p.strip() for p in (city, region) if p and p.strip()]
    if parts:
        return ", ".join(parts[:2])
    return (country or "your area").strip() or "your area"


def _get_weather_location() -> Optional[dict]:
    """Resolve weather location: env override → cached lookup → fresh IP lookup."""
    global _cached_weather_location, _weather_location_fetched_at

    lat_raw = os.getenv("WEATHER_LATITUDE", "").strip()
    lon_raw = os.getenv("WEATHER_LONGITUDE", "").strip()
    label_override = os.getenv("WEATHER_LOCATION_LABEL", "").strip()
    if lat_raw and lon_raw:
        try:
            return {
                "latitude": float(lat_raw),
                "longitude": float(lon_raw),
                "label": label_override or "your area",
            }
        except ValueError:
            log.warning("Invalid WEATHER_LATITUDE / WEATHER_LONGITUDE in environment")

    if (
        _cached_weather_location is not None
        and (time.time() - _weather_location_fetched_at) < _WEATHER_LOCATION_TTL_SECONDS
    ):
        return _cached_weather_location

    try:
        import urllib.request as _ureq
        with _ureq.urlopen(
            "https://ipwho.is/?fields=success,city,region,country,latitude,longitude",
            timeout=3,
        ) as resp:
            data = json.loads(resp.read().decode())
        if data.get("success") is True:
            location = {
                "latitude": float(data["latitude"]),
                "longitude": float(data["longitude"]),
                "label": label_override or _format_location_label(
                    str(data.get("city", "")),
                    str(data.get("region", "")),
                    str(data.get("country", "")),
                ),
            }
            _cached_weather_location = location
            _weather_location_fetched_at = time.time()
            return location
    except Exception as e:
        log.debug(f"IP-geolocation lookup failed: {e}")

    return _cached_weather_location


def _fetch_weather_string_sync() -> Optional[str]:
    """Sync weather fetch — safe to call from a threaded worker."""
    location = _get_weather_location()
    if not location:
        return None

    unit = os.getenv("WEATHER_UNIT", "fahrenheit").strip().lower()
    if unit not in ("fahrenheit", "celsius"):
        unit = "fahrenheit"
    unit_symbol = "°F" if unit == "fahrenheit" else "°C"

    try:
        import urllib.request as _ureq
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={location['latitude']}&longitude={location['longitude']}"
            f"&current=temperature_2m,weathercode&temperature_unit={unit}"
        )
        with _ureq.urlopen(url, timeout=3) as resp:
            current = json.loads(resp.read()).get("current", {})
        temp = current.get("temperature_2m")
        if temp is None:
            return None
        return f"Current weather in {location['label']}: {temp}{unit_symbol}"
    except Exception as e:
        log.debug(f"Weather fetch failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class ClaudeTask:
    id: str
    prompt: str
    status: str = "pending"  # pending, running, completed, failed, cancelled
    working_dir: str = "."
    pid: Optional[int] = None
    result: str = ""
    error: str = ""
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["started_at"] = self.started_at.isoformat() if self.started_at else None
        d["completed_at"] = self.completed_at.isoformat() if self.completed_at else None
        d["elapsed_seconds"] = self.elapsed_seconds
        return d

    @property
    def elapsed_seconds(self) -> float:
        if not self.started_at:
            return 0
        end = self.completed_at or datetime.now()
        return (end - self.started_at).total_seconds()


class IntakeFileRequest(BaseModel):
    name: str = Field(..., max_length=160)
    mime_type: str = Field(default="text/plain", max_length=120)
    content: str = Field(..., max_length=524288)


class VoiceSampleRequest(BaseModel):
    filename: str = Field(..., max_length=160)
    mime_type: str = Field(default="audio/webm", max_length=80)
    duration_seconds: float = Field(default=0, ge=0, le=300)
    data_base64: str = Field(..., max_length=8_000_000)


class TaskRequest(BaseModel):
    prompt: str
    working_dir: str = "."


# ---------------------------------------------------------------------------
# Claude Task Manager
# ---------------------------------------------------------------------------

class ClaudeTaskManager:
    """Manages background claude -p subprocesses."""

    def __init__(self, max_concurrent: int = 3):
        self._tasks: dict[str, ClaudeTask] = {}
        self._max_concurrent = max_concurrent
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._websockets: list[WebSocket] = []  # for push notifications

    def register_websocket(self, ws: WebSocket):
        if ws not in self._websockets:
            self._websockets.append(ws)

    def unregister_websocket(self, ws: WebSocket):
        if ws in self._websockets:
            self._websockets.remove(ws)

    async def _notify(self, message: dict):
        """Push a message to all connected WebSocket clients."""
        dead = []
        for ws in self._websockets:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._websockets.remove(ws)

    async def spawn(self, prompt: str, working_dir: str = ".") -> str:
        """Spawn a claude -p subprocess. Returns task_id. Non-blocking."""
        active = await self.get_active_count()
        if active >= self._max_concurrent:
            raise RuntimeError(
                f"Max concurrent tasks ({self._max_concurrent}) reached. "
                f"Wait for a task to complete or cancel one."
            )

        task_id = str(uuid.uuid4())[:8]
        task = ClaudeTask(
            id=task_id,
            prompt=prompt,
            working_dir=working_dir,
            status="pending",
        )
        self._tasks[task_id] = task

        # Fire and forget — the background coroutine updates the task
        asyncio.create_task(self._run_task(task))
        log.info(f"Spawned task {task_id}: {prompt[:80]}...")

        await self._notify({
            "type": "task_spawned",
            "task_id": task_id,
            "prompt": prompt,
        })

        return task_id

    def _generate_project_name(self, prompt: str) -> str:
        """Generate a kebab-case project folder name from the prompt."""
        import re
        # Extract key words
        words = re.sub(r'[^a-zA-Z0-9\s]', '', prompt.lower()).split()
        # Take first 3-4 meaningful words
        skip = {"a", "the", "an", "me", "build", "create", "make", "for", "with", "and", "to", "of"}
        meaningful = [w for w in words if w not in skip][:4]
        name = "-".join(meaningful) if meaningful else "jarvis-project"
        return name

    async def _run_task(self, task: ClaudeTask):
        """Open a Terminal window and run claude code visibly."""
        task.status = "running"
        task.started_at = datetime.now()

        # Create project directory if it doesn't exist
        work_dir = task.working_dir
        if work_dir == "." or not work_dir:
            # Create a new project folder on Desktop
            project_name = self._generate_project_name(task.prompt)
            work_dir = str(Path.home() / "Desktop" / project_name)
            os.makedirs(work_dir, exist_ok=True)
            task.working_dir = work_dir

        # Write the prompt to a temp file so we can pipe it to claude
        prompt_file = Path(work_dir) / ".jarvis_prompt.md"
        prompt_file.write_text(task.prompt)

        # Open Terminal.app with claude running in the project directory
        skip_flag = " --dangerously-skip-permissions" if _SKIP_PERMISSIONS else ""
        escaped_work_dir = applescript_escape(work_dir)
        applescript = f'''
        tell application "Terminal"
            activate
            set newTab to do script "cd {escaped_work_dir} && cat .jarvis_prompt.md | claude -p{skip_flag} | tee .jarvis_output.txt; echo '\\n--- JARVIS TASK COMPLETE ---'"
        end tell
        '''

        process = await asyncio.create_subprocess_exec(
            "osascript", "-e", applescript,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
        task.pid = process.pid

        # Monitor the output file for completion
        output_file = Path(work_dir) / ".jarvis_output.txt"
        start = time.time()
        timeout = 600  # 10 minutes

        stall_after = 60  # seconds without output growth before assuming completion
        while time.time() - start < timeout:
            await asyncio.sleep(5)
            if not output_file.exists():
                continue
            content = output_file.read_text()
            if "--- JARVIS TASK COMPLETE ---" in content:
                task.result = content.replace("--- JARVIS TASK COMPLETE ---", "").strip()
                task.status = "completed"
                break
            # Fallback: the marker can be lost (terminal closed, tee interrupted).
            # If there is real output and the file has stopped growing, treat the
            # task as finished rather than waiting out the full timeout.
            if content.strip():
                try:
                    mtime = output_file.stat().st_mtime
                except OSError:
                    continue
                if time.time() - mtime > stall_after:
                    task.result = content.strip()
                    task.status = "completed"
                    break
        else:
            task.status = "timed_out"
            task.error = f"Task timed out after {timeout}s"

        task.completed_at = datetime.now()

        # Notify via WebSocket
        await self._notify({
            "type": "task_complete",
            "task_id": task.id,
            "status": task.status,
            "summary": task.result[:200] if task.result else task.error,
        })

        # Clean up prompt file
        try:
            prompt_file.unlink()
        except:
            pass

        # Auto-QA on completed tasks
        if task.status == "completed":
            asyncio.create_task(self._run_qa(task))

    async def _run_qa(self, task: ClaudeTask, attempt: int = 1):
        """Run QA verification on a completed task, auto-retry on failure."""
        try:
            qa_result = await qa_agent.verify(task.prompt, task.result, task.working_dir)
            duration = task.elapsed_seconds

            if qa_result.passed:
                log.info(f"Task {task.id} passed QA: {qa_result.summary}")
                success_tracker.log_task("dev", task.prompt, True, attempt - 1, duration)
                await self._notify({
                    "type": "qa_result",
                    "task_id": task.id,
                    "passed": True,
                    "summary": qa_result.summary,
                })

                # Proactive suggestion after successful task
                suggestion = suggest_followup(
                    task_type="dev",
                    task_description=task.prompt,
                    working_dir=task.working_dir,
                    qa_result=qa_result,
                )
                if suggestion:
                    success_tracker.log_suggestion(task.id, suggestion.text)
                    await self._notify({
                        "type": "suggestion",
                        "task_id": task.id,
                        "text": suggestion.text,
                        "action_type": suggestion.action_type,
                        "action_details": suggestion.action_details,
                    })
            else:
                log.warning(f"Task {task.id} failed QA: {qa_result.issues}")
                if attempt < 3:
                    log.info(f"Auto-retrying task {task.id} (attempt {attempt + 1}/3)")
                    retry_result = await qa_agent.auto_retry(
                        task.prompt, qa_result.issues, task.working_dir, attempt,
                    )
                    if retry_result["status"] == "completed":
                        task.result = retry_result["result"]
                        # Re-verify
                        await self._run_qa(task, attempt + 1)
                    else:
                        success_tracker.log_task("dev", task.prompt, False, attempt, duration)
                        await self._notify({
                            "type": "qa_result",
                            "task_id": task.id,
                            "passed": False,
                            "summary": f"Failed after {attempt + 1} attempts: {qa_result.issues}",
                        })
                else:
                    success_tracker.log_task("dev", task.prompt, False, attempt, duration)
                    await self._notify({
                        "type": "qa_result",
                        "task_id": task.id,
                        "passed": False,
                        "summary": f"Failed QA after {attempt} attempts: {qa_result.issues}",
                    })
        except Exception as e:
            log.error(f"QA error for task {task.id}: {e}")

    async def get_status(self, task_id: str) -> Optional[ClaudeTask]:
        return self._tasks.get(task_id)

    async def list_tasks(self) -> list[ClaudeTask]:
        return list(self._tasks.values())

    async def get_active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status in ("pending", "running"))

    async def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.status not in ("pending", "running"):
            return False

        process = self._processes.get(task_id)
        if process:
            try:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    process.kill()
            except ProcessLookupError:
                pass

        task.status = "cancelled"
        task.completed_at = datetime.now()
        self._processes.pop(task_id, None)
        log.info(f"Cancelled task {task_id}")
        return True

    def get_active_tasks_summary(self) -> str:
        """Format active tasks for injection into the system prompt."""
        active = [t for t in self._tasks.values() if t.status in ("pending", "running")]
        completed_recent = [
            t for t in self._tasks.values()
            if t.status == "completed"
            and t.completed_at
            and (datetime.now() - t.completed_at).total_seconds() < 300
        ]

        if not active and not completed_recent:
            return "No active or recent tasks."

        lines = []
        for t in active:
            elapsed = f"{t.elapsed_seconds:.0f}s" if t.started_at else "queued"
            lines.append(f"- [{t.id}] RUNNING ({elapsed}): {t.prompt[:100]}")
        for t in completed_recent:
            lines.append(f"- [{t.id}] COMPLETED: {t.prompt[:60]} -> {t.result[:80]}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Project Scanner
# ---------------------------------------------------------------------------

async def scan_projects() -> list[dict]:
    """Quick scan of ~/Desktop for git repos (depth 1)."""
    projects = []
    desktop = DESKTOP_PATH

    if not desktop.exists():
        return projects

    try:
        for entry in sorted(desktop.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            git_dir = entry / ".git"
            if git_dir.exists():
                branch = "unknown"
                head_file = git_dir / "HEAD"
                try:
                    head_content = head_file.read_text().strip()
                    if head_content.startswith("ref: refs/heads/"):
                        branch = head_content.replace("ref: refs/heads/", "")
                except Exception:
                    pass

                projects.append({
                    "name": entry.name,
                    "path": str(entry),
                    "branch": branch,
                })
    except PermissionError:
        pass

    return projects


def format_projects_for_prompt(projects: list[dict]) -> str:
    if not projects:
        return "No projects found on Desktop."
    lines = []
    for p in projects:
        lines.append(f"- {p['name']} ({p['branch']}) @ {p['path']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Speech-to-Text Corrections
# ---------------------------------------------------------------------------

ACTION_KEYWORDS = {
    "open_terminal": ["open terminal", "launch terminal", "open claude code", "launch claude code"],
    "browse": ["browse", "search for", "look up", "google", "go to", "open website", "pull up"],
    "build": ["build", "create app", "make app", "generate project", "scaffold"],
}

STT_CORRECTIONS = {
    r"\bcloud code\b": "Claude Code",
    r"\bclock code\b": "Claude Code",
    r"\bquad code\b": "Claude Code",
    r"\bclawed code\b": "Claude Code",
    r"\bclod code\b": "Claude Code",
    r"\bcloud\b": "Claude",
    r"\bquad\b": "Claude",
    r"\btravis\b": "JARVIS",
    r"\bjarves\b": "JARVIS",
}


def apply_speech_corrections(text: str) -> str:
    """Fix common speech-to-text errors before processing."""
    import re as _stt_re
    result = text
    for pattern, replacement in STT_CORRECTIONS.items():
        result = _stt_re.sub(pattern, replacement, result, flags=_stt_re.IGNORECASE)
    return result


# ---------------------------------------------------------------------------
# LLM Intent Classifier (replaces keyword-based action detection)
# ---------------------------------------------------------------------------

async def classify_intent(text: str, client: anthropic.AsyncAnthropic) -> dict:
    """Classify every user message using Haiku LLM.

    Returns: {"action": "open_terminal|browse|build|chat", "target": "description"}
    """
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system=(
                "Classify this voice command. The user is talking to JARVIS, an AI assistant that can:\n"
                "- Open Terminal and run Claude Code (coding AI tool)\n"
                "- Open Chrome browser for web searches and URLs\n"
                "- Build software projects via Claude Code in Terminal\n"
                "- Research topics by opening Chrome search\n\n"
                "Note: speech-to-text may produce errors like \"Cloud\" for \"Claude\", "
                "\"Travis\" for \"JARVIS\", \"clock code\" for \"Claude Code\".\n\n"
                "Return ONLY valid JSON: {\"action\": \"open_terminal|browse|build|chat\", "
                "\"target\": \"description of what to do\"}\n"
                "open_terminal = user wants to open terminal or launch Claude Code\n"
                "browse = user wants to search the web, look something up, visit a URL\n"
                "build = user wants to create/build a software project\n"
                "chat = just conversation, questions, or anything else\n"
                "If unclear, default to \"chat\"."
            ),
            messages=[{"role": "user", "content": text}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(raw)
        return {
            "action": data.get("action", "chat"),
            "target": data.get("target", text),
        }
    except Exception as e:
        log.warning(f"Intent classification failed: {e}")
        return {"action": "chat", "target": text}


# ---------------------------------------------------------------------------
# Markdown Stripping for TTS
# ---------------------------------------------------------------------------

def strip_markdown_for_tts(text: str) -> str:
    """Strip ALL markdown from text before sending to TTS."""
    import re as _md_re
    result = text
    # Remove code blocks (``` ... ```)
    result = _md_re.sub(r"```[\s\S]*?```", "", result)
    # Remove inline code
    result = result.replace("`", "")
    # Remove bold/italic markers
    result = result.replace("**", "").replace("*", "")
    # Remove headers
    result = _md_re.sub(r"^#{1,6}\s*", "", result, flags=_md_re.MULTILINE)
    # Convert [text](url) to just text
    result = _md_re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", result)
    # Remove bullet points
    result = _md_re.sub(r"^\s*[-*+]\s+", "", result, flags=_md_re.MULTILINE)
    # Remove numbered lists
    result = _md_re.sub(r"^\s*\d+\.\s+", "", result, flags=_md_re.MULTILINE)
    # Double newlines to period
    result = _md_re.sub(r"\n{2,}", ". ", result)
    # Single newlines to space
    result = result.replace("\n", " ")
    # Clean up multiple spaces
    result = _md_re.sub(r"\s{2,}", " ", result)

    # Strip banned phrases
    banned = ["my apologies", "i apologize", "absolutely", "great question",
              "i'd be happy to", "of course", "how can i help",
              "is there anything else", "i should clarify", "let me know if",
              "feel free to"]
    result_lower = result.lower()
    for phrase in banned:
        idx = result_lower.find(phrase)
        while idx != -1:
            # Remove the phrase and any trailing comma/dash
            end = idx + len(phrase)
            if end < len(result) and result[end] in " ,—-":
                end += 1
            result = result[:idx] + result[end:]
            result_lower = result.lower()
            idx = result_lower.find(phrase)

    return result.strip().strip(",").strip("—").strip("-").strip()


# ---------------------------------------------------------------------------
# Action Tag Extraction (parse [ACTION:X] from LLM responses)
# ---------------------------------------------------------------------------

import re as _action_re


def extract_action(response: str) -> tuple[str, dict | None]:
    """Extract [ACTION:X] tag from LLM response.

    Returns (clean_text_for_tts, action_dict_or_none).
    """
    # Match the tag and its target up to the end of that line only, so any spoken
    # text the model places AFTER the tag (a common ordering) is preserved.
    match = _action_re.search(
        r'\[ACTION:(BUILD|BROWSE|RESEARCH|OPEN_TERMINAL|PROMPT_PROJECT|ADD_TASK|ADD_NOTE|COMPLETE_TASK|REMEMBER|CREATE_NOTE|READ_NOTE|SCREEN|PROFILE|RECOMMEND_SKILLS|ONBOARD_DONE|RUN_SKILL|MCP_CALL|CONTROL_CENTER|OPEN_APP|OPEN_PATH|SET_VOLUME|MEDIA|LOCK_SCREEN|CLIPBOARD|SCREENSHOT)\]\s*([^\n]*)',
        response,
    )
    if match:
        action_type = match.group(1).lower()
        action_target = match.group(2).strip()
        clean_text = (response[:match.start()] + response[match.end():]).strip()
        return clean_text, {"action": action_type, "target": action_target}
    return response, None


async def _execute_build(target: str):
    """Execute a build action from an LLM-embedded [ACTION:BUILD] tag."""
    try:
        await handle_build(target)
    except Exception as e:
        log.error(f"Build execution failed: {e}")


async def _execute_browse(target: str):
    """Execute a browse action from an LLM-embedded [ACTION:BROWSE] tag."""
    try:
        if target.startswith("http") or "." in target.split()[0]:
            await open_browser(target)
        else:
            from urllib.parse import quote
            await open_browser(f"https://www.google.com/search?q={quote(target)}")
    except Exception as e:
        log.error(f"Browse execution failed: {e}")


_DESKTOP_CONTROL_HANDLERS = {
    "open_app": system_control.open_app,
    "open_path": system_control.open_path,
    "set_volume": system_control.set_volume,
    "media": system_control.media,
    "clipboard": system_control.clipboard_copy,
}


async def _execute_desktop_control(action: str, target: str):
    """Run a cross-platform desktop-control action and record it in the action log."""
    try:
        if action == "lock_screen":
            result = await system_control.lock_screen()
        elif action == "screenshot":
            result = await system_control.take_screenshot()
        else:
            handler = _DESKTOP_CONTROL_HANDLERS.get(action)
            if handler is None:
                return
            result = await handler(target)
        action_log.record_action(
            "desktop_control",
            f"Desktop control: {action}",
            status="completed" if result.get("success") else "failed",
            risk="medium" if action == "lock_screen" else "low",
            target=target[:200],
            result=result,
        )
        if not result.get("success"):
            log.warning(f"Desktop control {action} failed: {result.get('confirmation')}")
        else:
            log.info(f"Desktop control {action}: {result.get('confirmation')}")
    except Exception as e:
        log.error(f"Desktop control {action} failed: {e}")


async def _execute_research(target: str, ws=None):
    """Execute research via claude -p in background. Opens report and speaks when done."""
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

        cmd = ["claude", "-p", "--output-format", "text"]
        if _SKIP_PERMISSIONS:
            cmd.append("--dangerously-skip-permissions")
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=path,
        )

        stdout, stderr = await asyncio.wait_for(
            process.communicate(input=prompt.encode()),
            timeout=300,
        )

        result = stdout.decode().strip()
        log.info(f"Research complete ({len(result)} chars)")

        recently_built.append({"name": name, "path": path, "time": time.time()})

        # Find and open any HTML report
        report = Path(path) / "report.html"
        if not report.exists():
            # Check for any HTML file
            html_files = list(Path(path).glob("*.html"))
            if html_files:
                report = html_files[0]

        if report.exists():
            await open_browser(f"file://{report}")
            log.info(f"Opened {report.name} in browser")

        # Notify via voice if WebSocket still connected
        if ws:
            try:
                notify_text = f"Research is complete, sir. Report is open in your browser."
                audio = await synthesize_speech(notify_text)
                if audio:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": notify_text})
                    await ws.send_json({"type": "status", "state": "idle"})
                    log.info(f"JARVIS: {notify_text}")
            except Exception:
                pass  # WebSocket might be gone

    except asyncio.TimeoutError:
        log.error("Research timed out after 5 minutes")
        if ws:
            try:
                audio = await synthesize_speech("Research timed out, sir. It was taking too long.")
                if audio:
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": "Research timed out, sir."})
            except Exception:
                pass
    except Exception as e:
        log.error(f"Research execution failed: {e}")


async def _focus_terminal_window(project_name: str):
    """Bring a Terminal window matching the project name to front."""
    escaped = applescript_escape(project_name)
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
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5)
    except Exception:
        pass


async def _execute_open_terminal():
    """Execute an open-terminal action from an LLM-embedded [ACTION:OPEN_TERMINAL] tag."""
    try:
        await handle_open_terminal()
    except Exception as e:
        log.error(f"Open terminal failed: {e}")


def _find_project_dir(project_name: str) -> str | None:
    """Find a project directory by name from cached projects or Desktop."""
    for p in cached_projects:
        if project_name.lower() in p.get("name", "").lower():
            return p.get("path")
    desktop = Path.home() / "Desktop"
    for d in desktop.iterdir():
        if d.is_dir() and project_name.lower() in d.name.lower():
            return str(d)
    return None


async def _execute_prompt_project(project_name: str, prompt: str, work_session: WorkSession, ws, dispatch_id: int = None, history: list[dict] = None, voice_state: dict = None):
    """Dispatch a prompt to Claude Code in a project directory.

    Runs entirely in the background. JARVIS returns to conversation mode
    immediately. When Claude Code finishes, JARVIS interrupts to report.
    """
    try:
        project_dir = _find_project_dir(project_name)

        # Register dispatch if not already registered
        if dispatch_id is None:
            dispatch_id = dispatch_registry.register(project_name, project_dir or "", prompt)

        if not project_dir:
            msg = f"Couldn't find the {project_name} project directory, sir."
            audio = await synthesize_speech(msg)
            if audio and ws:
                try:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                except Exception:
                    pass
            return

        # Use a SEPARATE session so we don't trap the main conversation
        dispatch = WorkSession()
        await dispatch.start(project_dir, project_name)

        # Bring matching Terminal window to front so user can watch
        asyncio.create_task(_focus_terminal_window(project_name))

        log.info(f"Dispatching to {project_name} in {project_dir}: {prompt[:80]}")
        dispatch_registry.update_status(dispatch_id, "building")

        # Run claude -p in background
        full_response = await dispatch.send(prompt)
        await dispatch.stop()

        # Auto-open any localhost URLs from response
        import re as _re
        # Check for the explicit RUNNING_AT marker first
        running_match = _re.search(r'RUNNING_AT=(https?://localhost:\d+)', full_response or "")
        if not running_match:
            running_match = _re.search(r'https?://localhost:\d+', full_response or "")
        if running_match:
            url = running_match.group(1) if running_match.lastindex else running_match.group(0)
            asyncio.create_task(_execute_browse(url))
            log.info(f"Auto-opening {url}")
            # Store URL in dispatch
            if dispatch_id:
                dispatch_registry.update_status(dispatch_id, "completed",
                    response=full_response[:2000], summary=f"Running at {url}")

        if not full_response or full_response.startswith("Hit a problem") or full_response.startswith("That's taking"):
            dispatch_registry.update_status(dispatch_id, "failed" if full_response else "timeout", response=full_response or "")
            msg = f"Sir, I ran into an issue with {project_name}. {full_response[:150] if full_response else 'No response received.'}"
        else:
            # Summarize via Haiku — don't read word for word
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
                        messages=[{"role": "user", "content": f"Project: {project_name}\nClaude Code reported:\n{full_response[:3000]}"}],
                    )
                    msg = summary.content[0].text
                except Exception:
                    msg = f"Sir, {project_name} finished. Here's the gist: {full_response[:200]}"
            else:
                msg = f"Sir, {project_name} is done. {full_response[:200]}"

        # Speak the result — skip if user has spoken recently to avoid audio collision
        log.info(f"Dispatch summary for {project_name}: {msg[:100]}")
        if voice_state and time.time() - voice_state["last_user_time"] < 3:
            log.info(f"Skipping dispatch audio for {project_name} — user spoke recently")
            # Result is still stored in history below so JARVIS can reference it
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

        # Store dispatch result in conversation history so JARVIS remembers it
        if history is not None:
            history.append({"role": "assistant", "content": f"[Dispatch result for {project_name}]: {msg}"})

        dispatch_registry.update_status(dispatch_id, "completed", response=full_response[:2000], summary=msg[:200])
        log.info(f"Project {project_name} dispatch complete ({len(full_response)} chars)")

    except Exception as e:
        log.error(f"Prompt project failed: {e}", exc_info=True)
        try:
            msg = f"Had trouble connecting to {project_name}, sir."
            audio = await synthesize_speech(msg)
            if audio and ws:
                await ws.send_json({"type": "status", "state": "speaking"})
                await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
        except Exception:
            pass


async def self_work_and_notify(session: WorkSession, prompt: str, ws):
    """Run claude -p in background and notify via voice when done."""
    try:
        full_response = await session.send(prompt)
        log.info(f"Background work complete ({len(full_response)} chars)")

        # Summarize and speak
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

            try:
                audio = await synthesize_speech(msg)
                if audio:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                    await ws.send_json({"type": "status", "state": "idle"})
                    log.info(f"JARVIS: {msg}")
            except Exception:
                pass
    except Exception as e:
        log.error(f"Background work failed: {e}")


# Smart greeting — track last greeting to avoid re-greeting on reconnect
_last_greeting_time: float = 0


# ---------------------------------------------------------------------------
# TTS (Fish Audio)
# ---------------------------------------------------------------------------

async def synthesize_speech(text: str) -> Optional[bytes]:
    """Generate speech audio via the active TTS provider (Fish Audio / ElevenLabs).

    Provider selection and keys are read live from the environment, so changes
    saved at runtime take effect without a restart. Usage accounting stays here.
    """
    audio = await provider_tts.synthesize(text)
    if audio is not None:
        _session_tokens["tts_calls"] += 1
        _append_usage_entry(0, 0, "tts")
    return audio


# ---------------------------------------------------------------------------
# LLM Response
# ---------------------------------------------------------------------------

async def generate_response(
    text: str,
    client: anthropic.AsyncAnthropic,
    task_mgr: ClaudeTaskManager,
    projects: list[dict],
    conversation_history: list[dict],
    last_response: str = "",
    session_summary: str = "",
) -> str:
    """Generate a JARVIS response using Anthropic API."""
    now = datetime.now()
    current_time = now.strftime("%A, %B %d, %Y at %I:%M %p")

    # Use cached weather
    weather_info = _ctx_cache.get("weather", "Weather data unavailable.")

    # Use cached context (refreshed in background, never blocks responses)
    screen_ctx = _ctx_cache["screen"]
    calendar_ctx = _ctx_cache["calendar"]
    mail_ctx = _ctx_cache["mail"]

    # Check if any lookups are in progress
    lookup_status = get_lookup_status()

    system = JARVIS_SYSTEM_PROMPT.format(
        current_time=current_time,
        weather_info=weather_info,
        screen_context=screen_ctx or "Not checked yet.",
        calendar_context=calendar_ctx,
        mail_context=mail_ctx,
        active_tasks=task_mgr.get_active_tasks_summary(),
        dispatch_context=dispatch_registry.format_for_prompt(),
        known_projects=format_projects_for_prompt(projects),
        user_name=USER_NAME,
        project_dir=PROJECT_DIR,
    )
    system += f"\n\n{_personalization_prompt()}"

    if lookup_status:
        system += f"\n\nACTIVE LOOKUPS:\n{lookup_status}\nIf asked about progress, report this status."

    # Inject relevant memories and tasks
    memory_ctx = build_memory_context(text)
    if memory_ctx:
        system += f"\n\nJARVIS MEMORY:\n{memory_ctx}"

    # User profile (learned during onboarding)
    profile_ctx = onboarding_system.profile_prompt()
    if profile_ctx:
        system += f"\n\n{profile_ctx}"

    # Onboarding takes priority — if active, steer the discovery conversation
    onboarding_ctx = onboarding_system.onboarding_prompt()
    if onboarding_ctx:
        system += f"\n\n{onboarding_ctx}"

    # Active skills and a lightweight menu of what else can be enabled
    skills_ctx = skills_system.enabled_skills_prompt()
    if skills_ctx:
        system += f"\n\n{skills_ctx}"
    if onboarding_ctx:
        # Only surface the broader catalog while onboarding, to keep prompts lean
        catalog_ctx = skills_system.catalog_index_prompt()
        if catalog_ctx:
            system += f"\n\n{catalog_ctx}"

    # Connected external tools via MCP
    mcp_ctx = mcp_registry.mcp_prompt()
    if mcp_ctx:
        system += f"\n\n{mcp_ctx}"

    # Three-tier memory — inject rolling summary of earlier conversation
    if session_summary:
        system += f"\n\nSESSION CONTEXT (earlier in this conversation):\n{session_summary}"

    # Self-awareness — remind JARVIS of last response to avoid repetition
    if last_response:
        system += f'\n\nYOUR LAST RESPONSE (do not repeat this):\n"{last_response[:150]}"'

    # Use conversation history — keep the last 20 messages for context
    # (older conversation is captured in session_summary)
    messages = conversation_history[-20:]
    # If the last message isn't the current user text, add it
    if not messages or messages[-1].get("content") != text:
        messages = messages + [{"role": "user", "content": text}]

    # Route the conversational brain through the active provider. Claude remains
    # the default and still drives actions/classification elsewhere; other brains
    # may be weaker at the [ACTION:X] protocol (surfaced as a note in the UI).
    provider = provider_config.active_llm_provider()
    model = provider_config.active_llm_model(provider)
    if provider == "anthropic" and not os.getenv(provider_config.model_env_key("anthropic"), "").strip():
        # Low-latency default for the voice loop; an explicit user override wins.
        model = "claude-haiku-4-5-20251001"
    return await provider_llm.complete(
        provider=provider,
        model=model,
        system=system,
        messages=messages,
        max_tokens=250,  # Extra room for [ACTION:X] tags
        anthropic_client=client,
        on_usage=_record_llm_usage,
    )


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

# Shared state
task_manager = ClaudeTaskManager(max_concurrent=3)
anthropic_client: Optional[anthropic.AsyncAnthropic] = None
cached_projects: list[dict] = []
recently_built: list[dict] = []  # [{"name": str, "path": str, "time": float}]
dispatch_registry = DispatchRegistry()

# Usage tracking — logs every call with timestamp, persists to disk
_USAGE_FILE = Path(__file__).parent / "data" / "usage_log.jsonl"
_session_start = time.time()
_session_tokens = {"input": 0, "output": 0, "api_calls": 0, "tts_calls": 0}


def _append_usage_entry(input_tokens: int, output_tokens: int, call_type: str = "api"):
    """Append a usage entry with timestamp to the log file."""
    try:
        _USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        entry = {
            "ts": time.time(),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "type": call_type,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        with open(_USAGE_FILE, "a") as f:
            f.write(_json.dumps(entry) + "\n")
    except Exception:
        pass


def _get_usage_for_period(seconds: float | None = None) -> dict:
    """Sum usage from the log file for a time period. None = all time."""
    import json as _json
    totals = {"input_tokens": 0, "output_tokens": 0, "api_calls": 0, "tts_calls": 0}
    cutoff = (time.time() - seconds) if seconds else 0
    try:
        if _USAGE_FILE.exists():
            for line in _USAGE_FILE.read_text().strip().split("\n"):
                if not line:
                    continue
                entry = _json.loads(line)
                if entry["ts"] >= cutoff:
                    totals["input_tokens"] += entry.get("input_tokens", 0)
                    totals["output_tokens"] += entry.get("output_tokens", 0)
                    if entry.get("type") == "tts":
                        totals["tts_calls"] += 1
                    else:
                        totals["api_calls"] += 1
    except Exception:
        pass
    return totals


def _cost_from_tokens(input_t: int, output_t: int) -> float:
    # Claude Haiku 4.5 list pricing ($1/MTok in, $5/MTok out); used as an
    # approximation when a non-Anthropic brain is active.
    return (input_t / 1_000_000) * 1.00 + (output_t / 1_000_000) * 5.00


def track_usage(response):
    """Track token usage from an Anthropic API response."""
    inp = getattr(response.usage, "input_tokens", 0) if hasattr(response, "usage") else 0
    out = getattr(response.usage, "output_tokens", 0) if hasattr(response, "usage") else 0
    _record_llm_usage(inp, out)


def _record_llm_usage(input_tokens: int, output_tokens: int):
    """Usage callback shared by all LLM providers (Anthropic + httpx-based)."""
    _session_tokens["input"] += input_tokens or 0
    _session_tokens["output"] += output_tokens or 0
    _session_tokens["api_calls"] += 1
    _append_usage_entry(input_tokens or 0, output_tokens or 0, "api")


async def summarize(prompt: str, system: str = "", max_tokens: int = 150) -> str:
    """Summarize/condense text, preferring Claude but falling back to the active
    provider when no Anthropic key is configured. Returns "" on failure so callers
    can skip gracefully. Keeps background work (session summary, memory extraction,
    dispatch summaries) alive even when a non-Claude brain is in use.
    """
    messages = [{"role": "user", "content": prompt}]
    if anthropic_client is not None:
        return await provider_llm.complete(
            provider="anthropic", model="claude-haiku-4-5-20251001",
            system=system, messages=messages, max_tokens=max_tokens,
            anthropic_client=anthropic_client, on_usage=_record_llm_usage,
        )
    provider = provider_config.active_llm_provider()
    if provider == "anthropic":
        return ""  # no Claude key and no alternate brain selected
    text = await provider_llm.complete(
        provider=provider, model=provider_config.active_llm_model(provider),
        system=system, messages=messages, max_tokens=max_tokens, on_usage=_record_llm_usage,
    )
    return "" if text == provider_llm.FALLBACK else text


def get_usage_summary() -> str:
    """Get a voice-friendly usage summary with time breakdowns."""
    uptime_min = int((time.time() - _session_start) / 60)

    session = _session_tokens
    today = _get_usage_for_period(86400)
    week = _get_usage_for_period(86400 * 7)
    all_time = _get_usage_for_period(None)

    session_cost = _cost_from_tokens(session["input"], session["output"])
    today_cost = _cost_from_tokens(today["input_tokens"], today["output_tokens"])
    all_cost = _cost_from_tokens(all_time["input_tokens"], all_time["output_tokens"])

    parts = [f"This session: {uptime_min} minutes, {session['api_calls']} calls, ${session_cost:.2f}."]

    if today["api_calls"] > session["api_calls"]:
        parts.append(f"Today total: {today['api_calls']} calls, ${today_cost:.2f}.")

    if all_time["api_calls"] > today["api_calls"]:
        parts.append(f"All time: {all_time['api_calls']} calls, ${all_cost:.2f}.")

    return " ".join(parts)

# Background context cache — never blocks responses
_ctx_cache = {
    "screen": "",
    "calendar": "No calendar data yet.",
    "mail": "No mail data yet.",
    "weather": "Weather data unavailable.",
}


def _refresh_context_sync():
    """Run in a SEPARATE THREAD — refreshes screen/calendar/mail context.

    This runs completely off the async event loop so it never blocks responses.
    """
    import threading

    def _worker():
        while True:
            try:
                # Screen — fast
                try:
                    proc = __import__("subprocess").run(
                        ["osascript", "-e", '''
set windowList to ""
tell application "System Events"
    set frontApp to name of first application process whose frontmost is true
    set visibleApps to every application process whose visible is true
    repeat with proc in visibleApps
        set appName to name of proc
        try
            set winCount to count of windows of proc
            if winCount > 0 then
                repeat with w in (windows of proc)
                    try
                        set winTitle to name of w
                        if winTitle is not "" and winTitle is not missing value then
                            set windowList to windowList & appName & "|||" & winTitle & "|||" & (appName = frontApp) & linefeed
                        end if
                    end try
                end repeat
            end if
        end try
    end repeat
end tell
return windowList
'''],
                        capture_output=True, text=True, timeout=5
                    )
                    if proc.returncode == 0 and proc.stdout.strip():
                        windows = []
                        for line in proc.stdout.strip().split("\n"):
                            parts = line.strip().split("|||")
                            if len(parts) >= 3:
                                windows.append({
                                    "app": parts[0].strip(),
                                    "title": parts[1].strip(),
                                    "frontmost": parts[2].strip().lower() == "true",
                                })
                        if windows:
                            _ctx_cache["screen"] = format_windows_for_context(windows)
                except Exception:
                    pass

            except Exception as e:
                log.debug(f"Context thread error: {e}")

            # Weather — refresh every loop (30s is fine, API is fast).
            # Location resolves from env override → cached lookup → IP geolocation.
            weather_string = _fetch_weather_string_sync()
            if weather_string:
                _ctx_cache["weather"] = weather_string

            time.sleep(30)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    log.info("Context refresh thread started")


@asynccontextmanager
async def lifespan(application: FastAPI):
    global anthropic_client, cached_projects
    if ANTHROPIC_API_KEY:
        anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    else:
        log.warning("ANTHROPIC_API_KEY not set — LLM features disabled")
    cached_projects = []

    # Start context refresh in a separate thread (never touches event loop)
    _refresh_context_sync()
    log.info("JARVIS server starting")

    yield


app = FastAPI(title="JARVIS Server", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -- REST Endpoints --------------------------------------------------------

@app.get("/api/health")
async def health():
    return {"status": "online", "name": "JARVIS", "version": "0.1.0"}


@app.get("/api/tts-test")
async def tts_test():
    """Generate a test audio clip for debugging."""
    audio = await synthesize_speech("Testing audio, sir.")
    if audio:
        return {"audio": base64.b64encode(audio).decode()}
    return {"audio": None, "error": "TTS failed"}





def _safe_count(label: str, getter, default):
    try:
        return getter()
    except Exception as exc:
        log.debug("briefing %s unavailable: %s", label, exc)
        return default


def _compact_task(task: dict) -> dict:
    return {
        "id": task.get("id"),
        "title": task.get("title", ""),
        "priority": task.get("priority", "medium"),
        "due_date": task.get("due_date") or "",
        "project": task.get("project") or "",
    }


def _compact_memory(memory: dict) -> dict:
    return {
        "id": memory.get("id"),
        "type": memory.get("type", "fact"),
        "content": memory.get("content", ""),
        "importance": memory.get("importance", 5),
    }


@app.get("/api/briefing")
async def api_briefing():
    """Personal assistant briefing assembled from local context and connectors.

    This endpoint is intentionally deterministic and cheap: it avoids LLM calls
    so the UI can refresh the user's operational picture frequently.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    open_task_items = _safe_count("tasks", lambda: get_open_tasks(), [])
    high_priority = [t for t in open_task_items if str(t.get("priority", "")).lower() == "high"]
    due_today = [t for t in open_task_items if (t.get("due_date") or "") == today]
    overdue = [t for t in open_task_items if (t.get("due_date") or "") and (t.get("due_date") or "") < today]

    try:
        calendar_events = await get_todays_events()
    except Exception as exc:
        log.debug("briefing calendar unavailable: %s", exc)
        calendar_events = []
    try:
        unread = await get_unread_count()
    except Exception as exc:
        log.debug("briefing mail unavailable: %s", exc)
        unread = {"count": 0, "available": False}
    memories = _safe_count("memories", lambda: get_important_memories(5), [])
    stats = _safe_count("memory stats", memory_stats, {})
    mcp_servers = _safe_count("mcp", mcp_registry.connected_servers, [])

    recommendations = []
    if overdue:
        recommendations.append(f"Clear {len(overdue)} overdue task{'s' if len(overdue) != 1 else ''} first.")
    if high_priority:
        recommendations.append(f"Protect focus time for {len(high_priority)} high-priority task{'s' if len(high_priority) != 1 else ''}.")
    if calendar_events:
        recommendations.append(f"Calendar has {len(calendar_events)} event{'s' if len(calendar_events) != 1 else ''} today; leave buffer around meetings.")
    if int(unread.get("count") or 0) > 0:
        recommendations.append(f"Triage {int(unread.get('count') or 0)} unread email{'s' if int(unread.get('count') or 0) != 1 else ''} after the first focus block.")
    if not mcp_servers:
        recommendations.append("Connect one MCP tool to unlock richer external actions.")
    if not recommendations:
        recommendations.append("No obvious fires, sir — an unusually civilised state of affairs.")

    return {
        "generated_at": datetime.now().isoformat(),
        "tasks": {
            "open": len(open_task_items),
            "high_priority": len(high_priority),
            "due_today": [_compact_task(t) for t in due_today[:5]],
            "overdue": [_compact_task(t) for t in overdue[:5]],
            "focus": [_compact_task(t) for t in (overdue + high_priority + open_task_items)[:5]],
        },
        "calendar": {"today_count": len(calendar_events), "events": calendar_events[:5]},
        "email": unread,
        "memory": {"stats": stats, "important": [_compact_memory(m) for m in memories]},
        "connectivity": {
            "mcp_connected": len(mcp_servers),
            "mcp_servers": [{"id": s.get("id"), "name": s.get("name"), "category": s.get("category")} for s in mcp_servers[:6]],
            "providers_configured": [p["id"] for p in providers_for_status() if p.get("configured")],
        },
        "recommendations": recommendations[:4],
    }

@app.get("/api/system")
async def api_system():
    """Lightweight HUD telemetry for the browser mission-control panel."""
    uptime = int(time.time() - _session_start)
    try:
        load_average = os.getloadavg()[0]
    except (AttributeError, OSError):
        load_average = None

    disk = shutil.disk_usage(Path(__file__).parent)
    return {
        "status": "online",
        "uptime_seconds": uptime,
        "load_average": load_average,
        "disk_free_gb": round(disk.free / (1024 ** 3), 2),
        "disk_total_gb": round(disk.total / (1024 ** 3), 2),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "connected_clients": len(task_manager._websockets),
    }


@app.post("/api/intake-file")
async def api_intake_file(req: IntakeFileRequest):
    """Save a small dropped text/code file and index a summary as a JARVIS note."""
    safe_name = Path(req.name).name.replace("/", "_").replace("\\", "_")
    if not safe_name:
        return JSONResponse(status_code=400, content={"error": "Invalid file name"})

    upload_dir = Path(__file__).parent / "data" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = upload_dir / f"{stamp}-{safe_name}"
    target.write_text(req.content, encoding="utf-8", errors="replace")

    preview = req.content[:1800].strip()
    title = f"Uploaded file: {safe_name}"
    note = (
        f"File uploaded through the JARVIS mission-control HUD.\n"
        f"Path: {target}\n"
        f"MIME: {req.mime_type}\n\n"
        f"Preview:\n{preview}"
    )
    try:
        create_note(title=title, content=note, topic="uploads", tags=["upload", "hud", safe_name])
    except Exception as e:
        log.warning(f"Could not index uploaded file note: {e}")

    return {
        "status": "stored",
        "name": safe_name,
        "path": str(target),
        "bytes": len(req.content.encode("utf-8")),
        "preview": preview[:500],
    }


@app.get("/api/usage")
async def api_usage():
    uptime = int(time.time() - _session_start)
    today = _get_usage_for_period(86400)
    week = _get_usage_for_period(86400 * 7)
    month = _get_usage_for_period(86400 * 30)
    all_time = _get_usage_for_period(None)
    return {
        "session": {**_session_tokens, "uptime_seconds": uptime},
        "today": {**today, "cost_usd": round(_cost_from_tokens(today["input_tokens"], today["output_tokens"]), 4)},
        "week": {**week, "cost_usd": round(_cost_from_tokens(week["input_tokens"], week["output_tokens"]), 4)},
        "month": {**month, "cost_usd": round(_cost_from_tokens(month["input_tokens"], month["output_tokens"]), 4)},
        "all_time": {**all_time, "cost_usd": round(_cost_from_tokens(all_time["input_tokens"], all_time["output_tokens"]), 4)},
    }


@app.get("/api/tasks")
async def api_list_tasks():
    tasks = await task_manager.list_tasks()
    return {"tasks": [t.to_dict() for t in tasks]}


@app.get("/api/tasks/{task_id}")
async def api_get_task(task_id: str):
    task = await task_manager.get_status(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"error": "Task not found"})
    return {"task": task.to_dict()}


@app.post("/api/tasks")
async def api_create_task(req: TaskRequest):
    try:
        task_id = await task_manager.spawn(req.prompt, req.working_dir)
        return {"task_id": task_id, "status": "spawned"}
    except RuntimeError as e:
        return JSONResponse(status_code=429, content={"error": str(e)})


@app.delete("/api/tasks/{task_id}")
async def api_cancel_task(task_id: str):
    cancelled = await task_manager.cancel(task_id)
    if not cancelled:
        return JSONResponse(
            status_code=404,
            content={"error": "Task not found or not cancellable"},
        )
    return {"task_id": task_id, "status": "cancelled"}


@app.get("/api/projects")
async def api_list_projects():
    global cached_projects
    cached_projects = await scan_projects()
    return {"projects": cached_projects}


# -- Fast Action Detection (no LLM call) -----------------------------------

def _scan_projects_sync() -> list[dict]:
    """Synchronous Desktop scan — runs in executor."""
    projects = []
    desktop = Path.home() / "Desktop"
    try:
        for entry in desktop.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                projects.append({"name": entry.name, "path": str(entry), "branch": ""})
    except Exception:
        pass
    return projects


def detect_action_fast(text: str) -> dict | None:
    """Keyword-based action detection — ONLY for short, obvious commands.

    Everything else goes to the LLM which uses [ACTION:X] tags when it decides
    to act based on conversational understanding.
    """
    t = text.lower().strip()
    words = t.split()

    # Only trigger on SHORT, clear commands (< 12 words)
    if len(words) > 12:
        return None  # Long messages are conversation, not commands

    # Screen requests — checked BEFORE project matching to prevent misrouting
    if any(p in t for p in ["look at my screen", "what's on my screen", "whats on my screen",
                             "what am i looking at", "what do you see", "see my screen",
                             "what's running on my", "whats running on my", "check my screen"]):
        return {"action": "describe_screen"}

    # Terminal / Claude Code — explicit open requests
    if any(w in t for w in ["open claude", "start claude", "launch claude", "run claude"]):
        return {"action": "open_terminal"}

    # Show recent build
    if any(w in t for w in ["show me what you built", "pull up what you made", "open what you built"]):
        return {"action": "show_recent"}

    # Screen awareness — explicit look/see requests
    if any(p in t for p in ["what's on my screen", "whats on my screen", "what do you see",
                             "can you see my screen", "look at my screen", "what am i looking at",
                             "what's open", "whats open", "what apps are open"]):
        return {"action": "describe_screen"}

    # Calendar — explicit schedule requests
    if any(p in t for p in ["what's my schedule", "whats my schedule", "what's on my calendar",
                             "whats on my calendar", "do i have any meetings", "any meetings",
                             "what's next on my calendar", "my schedule today",
                             "what do i have today", "my calendar", "upcoming meetings",
                             "next meeting", "what's my next meeting"]):
        return {"action": "check_calendar"}

    # Mail — explicit email requests
    if any(p in t for p in ["check my email", "check my mail", "any new emails", "any new mail",
                             "unread emails", "unread mail", "what's in my inbox",
                             "whats in my inbox", "read my email", "read my mail",
                             "any emails", "any mail", "email update", "mail update"]):
        return {"action": "check_mail"}

    # Dispatch / build status check
    if any(p in t for p in ["where are we", "where were we", "project status", "how's the build",
                             "hows the build", "status update", "status report", "where is that",
                             "how's it going with", "hows it going with", "is it done",
                             "is that done", "what happened with"]):
        return {"action": "check_dispatch"}

    # Task list check
    if any(p in t for p in ["what's on my list", "whats on my list", "my tasks", "my to do",
                             "my todo", "what do i need to do", "open tasks", "task list"]):
        return {"action": "check_tasks"}

    # Usage / cost check
    if any(p in t for p in ["usage", "how much have you cost", "how much am i spending",
                             "what's the cost", "whats the cost", "api cost", "token usage",
                             "how expensive", "what's my bill"]):
        return {"action": "check_usage"}

    return None  # Everything else goes to the LLM for conversational routing


# -- Action Handlers -------------------------------------------------------

async def handle_open_terminal() -> str:
    claude_cmd = "claude --dangerously-skip-permissions" if _SKIP_PERMISSIONS else "claude"
    result = await open_terminal(claude_cmd)
    return result["confirmation"]


async def handle_build(target: str) -> str:
    name = _generate_project_name(target)
    path = str(Path.home() / "Desktop" / name)
    os.makedirs(path, exist_ok=True)

    # Write CLAUDE.md with clear instructions
    claude_md = Path(path) / "CLAUDE.md"
    claude_md.write_text(f"# Task\n\n{target}\n\nBuild this completely. If web app, make index.html work standalone.\n")

    # Write prompt to a file, then pipe it to claude -p
    # This avoids all shell escaping issues
    prompt_file = Path(path) / ".jarvis_prompt.txt"
    prompt_file.write_text(target)

    skip_flag = " --dangerously-skip-permissions" if _SKIP_PERMISSIONS else ""
    escaped_path = applescript_escape(path)
    script = (
        'tell application "Terminal"\n'
        "    activate\n"
        f'    do script "cd {escaped_path} && cat .jarvis_prompt.txt | claude -p{skip_flag}"\n'
        "end tell"
    )
    await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    recently_built.append({"name": name, "path": path, "time": time.time()})
    return f"On it, sir. Claude Code is working in {name}."


async def handle_show_recent() -> str:
    if not recently_built:
        return "Nothing built recently, sir."
    last = recently_built[-1]
    project_path = Path(last["path"])

    # Try to find the best file to open
    for name in ["report.html", "index.html"]:
        f = project_path / name
        if f.exists():
            await open_browser(f"file://{f}")
            return f"Opened {name} from {last['name']}, sir."

    # Try any HTML file
    html_files = list(project_path.glob("*.html"))
    if html_files:
        await open_browser(f"file://{html_files[0]}")
        return f"Opened {html_files[0].name} from {last['name']}, sir."

    # Fall back to opening the folder in Finder
    escaped_last_path = applescript_escape(last["path"])
    script = f'tell application "Finder"\nactivate\nopen POSIX file "{escaped_last_path}"\nend tell'
    await asyncio.create_subprocess_exec("osascript", "-e", script, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    return f"Opened the {last['name']} folder in Finder, sir."


# ---------------------------------------------------------------------------
# Background lookup system — spawns slow tasks, reports back via voice
# ---------------------------------------------------------------------------

# Track active lookups so JARVIS can report status
_active_lookups: dict[str, dict] = {}  # id -> {"type": str, "status": str, "started": float}


async def _lookup_and_report(lookup_type: str, lookup_fn, ws, history: list[dict] = None, voice_state: dict = None):
    """Run a slow lookup, then speak the result back.

    JARVIS stays conversational — this runs completely off the main path.
    """
    lookup_id = str(uuid.uuid4())[:8]
    _active_lookups[lookup_id] = {
        "type": lookup_type,
        "status": "working",
        "started": time.time(),
    }

    try:
        # Run the async lookup directly — these functions already use
        # asyncio.create_subprocess_exec so they don't block the event loop
        result_text = await asyncio.wait_for(
            lookup_fn(),
            timeout=30,
        )

        _active_lookups[lookup_id]["status"] = "done"

        # Speak the result — skip audio if user spoke recently to avoid collision
        if voice_state and time.time() - voice_state["last_user_time"] < 3:
            log.info(f"Skipping lookup audio for {lookup_type} — user spoke recently")
            # Result is still stored in history below
        else:
            tts = strip_markdown_for_tts(result_text)
            audio = await synthesize_speech(tts)
            try:
                await ws.send_json({"type": "status", "state": "speaking"})
                if audio:
                    await ws.send_json({"type": "audio", "data": audio, "text": result_text})
                else:
                    await ws.send_json({"type": "text", "text": result_text})
                await ws.send_json({"type": "status", "state": "idle"})
            except Exception:
                pass

        log.info(f"Lookup {lookup_type} complete: {result_text[:80]}")

        # Store lookup result in conversation history so JARVIS remembers it
        if history is not None:
            history.append({"role": "assistant", "content": f"[{lookup_type} check]: {result_text}"})

    except asyncio.TimeoutError:
        _active_lookups[lookup_id]["status"] = "timeout"
        try:
            fallback = f"That {lookup_type} check is taking too long, sir. The data may still be syncing."
            audio = await synthesize_speech(fallback)
            await ws.send_json({"type": "status", "state": "speaking"})
            if audio:
                await ws.send_json({"type": "audio", "data": audio, "text": fallback})
            await ws.send_json({"type": "status", "state": "idle"})
        except Exception:
            pass
    except Exception as e:
        _active_lookups[lookup_id]["status"] = "error"
        log.warning(f"Lookup {lookup_type} failed: {e}")
    finally:
        # Clean up after 60s
        await asyncio.sleep(60)
        _active_lookups.pop(lookup_id, None)


async def _do_calendar_lookup() -> str:
    """Slow calendar fetch — runs in thread."""
    await refresh_calendar_cache()
    events = await get_todays_events()
    if events:
        _ctx_cache["calendar"] = format_events_for_context(events)
    return format_schedule_summary(events)


async def _do_mail_lookup() -> str:
    """Slow mail fetch — runs in thread."""
    unread_info = await get_unread_count()
    if isinstance(unread_info, dict):
        _ctx_cache["mail"] = format_unread_summary(unread_info)
        if unread_info["total"] == 0:
            return "Inbox is clear, sir. No unread messages."
        unread_msgs = await get_unread_messages(count=5)
        summary = format_unread_summary(unread_info)
        if unread_msgs:
            top = unread_msgs[:3]
            details = ". ".join(
                f"{_short_sender(m['sender'])} regarding {m['subject']}"
                for m in top
            )
            return f"{summary} Most recent: {details}."
        return summary
    return "Couldn't reach Mail at the moment, sir."


async def _do_screen_lookup() -> str:
    """Screen describe — runs in thread."""
    if anthropic_client:
        return await describe_screen(anthropic_client)
    windows = await get_active_windows()
    if windows:
        apps = set(w["app"] for w in windows)
        active = next((w for w in windows if w["frontmost"]), None)
        result = f"You have {', '.join(apps)} open."
        if active:
            result += f" Currently focused on {active['app']}: {active['title']}."
        return result
    return "Couldn't see the screen, sir."


def get_lookup_status() -> str:
    """Get status of active lookups for when user asks 'how's that coming'."""
    if not _active_lookups:
        return ""
    active = [v for v in _active_lookups.values() if v["status"] == "working"]
    if not active:
        return ""
    parts = []
    for lookup in active:
        elapsed = int(time.time() - lookup["started"])
        parts.append(f"{lookup['type']} check ({elapsed}s)")
    return "Currently working on: " + ", ".join(parts)


def _short_sender(sender: str) -> str:
    """Extract just the name from an email sender string."""
    if "<" in sender:
        return sender.split("<")[0].strip().strip('"')
    if "@" in sender:
        return sender.split("@")[0]
    return sender


async def handle_browse(text: str, target: str) -> str:
    """Open a URL directly or search. Smart about detecting URLs in speech."""
    import re
    from urllib.parse import quote

    browser = "firefox" if "firefox" in text.lower() else "chrome"
    combined = text.lower()

    # 1. Try to find a URL or domain in the text
    # Match things like "joetmd.com", "google.com/maps", "https://example.com"
    url_pattern = r'(?:https?://)?(?:www\.)?([a-zA-Z0-9][-a-zA-Z0-9]*(?:\.[a-zA-Z]{2,})+(?:/[^\s]*)?)'
    url_match = re.search(url_pattern, text, re.IGNORECASE)

    if url_match:
        domain = url_match.group(0)
        if not domain.startswith("http"):
            domain = "https://" + domain
        await open_browser(domain, browser)
        return f"Opened {url_match.group(0)}, sir."

    # 2. Check for spoken domains that speech-to-text mangled
    # "Joe tmd.com" → "joetmd.com", "roofo.co" etc.
    # Try joining words that end/start with a dot pattern
    words = text.split()
    for i, word in enumerate(words):
        # Look for word ending with common TLD
        if re.search(r'\.(com|co|io|ai|org|net|dev|app)$', word, re.IGNORECASE):
            # This word IS a domain — might have spaces before it
            domain = word
            # Check if previous word should be joined (e.g., "Joe tmd.com" → "joetmd.com" is tricky)
            if not domain.startswith("http"):
                domain = "https://" + domain
            await open_browser(domain, browser)
            return f"Opened {word}, sir."

    # 3. Fall back to Google search with cleaned query
    query = target
    for prefix in ["search for", "look up", "google", "find me", "pull up", "open chrome",
                    "open firefox", "open browser", "go to", "can you", "in the browser",
                    "can you go to", "please"]:
        query = query.lower().replace(prefix, "").strip()
    # Remove filler words
    query = re.sub(r'\b(can|you|the|in|to|a|an|for|me|my|please)\b', '', query).strip()
    query = re.sub(r'\s+', ' ', query).strip()

    if not query:
        query = target

    url = f"https://www.google.com/search?q={quote(query)}"
    await open_browser(url, browser)
    return "Searching for that, sir."


async def handle_research(text: str, target: str, client: anthropic.AsyncAnthropic) -> str:
    """Deep research with Opus — write results to HTML, open in browser."""
    try:
        research_response = await client.messages.create(
            model="claude-opus-4-6",
            max_tokens=2000,
            system=f"You are JARVIS, researching a topic for {USER_NAME}. Be thorough, organized, and cite sources where possible.",
            messages=[{"role": "user", "content": f"Research this thoroughly:\n\n{target}"}],
        )
        research_text = research_response.content[0].text

        import html as _html
        html_content = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>JARVIS Research: {_html.escape(target[:60])}</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 800px; margin: 40px auto; padding: 20px; background: #0a0a0a; color: #e0e0e0; line-height: 1.7; }}
h1 {{ color: #0ea5e9; font-size: 1.4em; border-bottom: 1px solid #222; padding-bottom: 10px; }}
h2 {{ color: #38bdf8; font-size: 1.1em; margin-top: 24px; }}
a {{ color: #0ea5e9; }}
pre {{ background: #111; padding: 12px; border-radius: 6px; overflow-x: auto; }}
code {{ background: #111; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }}
blockquote {{ border-left: 3px solid #0ea5e9; margin-left: 0; padding-left: 16px; color: #aaa; }}
</style>
</head><body>
<h1>Research: {_html.escape(target[:80])}</h1>
<div>{research_text.replace(chr(10), '<br>')}</div>
<hr style="border-color:#222;margin-top:40px">
<p style="color:#555;font-size:0.8em">Researched by JARVIS using Claude Opus &bull; {datetime.now().strftime('%B %d, %Y %I:%M %p')}</p>
</body></html>"""

        results_file = Path.home() / "Desktop" / ".jarvis_research.html"
        results_file.write_text(html_content)

        browser_name = "firefox" if "firefox" in text.lower() else "chrome"
        await open_browser(f"file://{results_file}", browser_name)

        # Short voice summary via Haiku
        summary = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            system="Summarize this research in ONE sentence for voice. No markdown.",
            messages=[{"role": "user", "content": research_text[:2000]}],
        )
        return summary.content[0].text + " Full results are in your browser, sir."

    except Exception as e:
        log.error(f"Research failed: {e}")
        from urllib.parse import quote
        await open_browser(f"https://www.google.com/search?q={quote(target)}")
        return "Pulled up a search for that, sir."


# -- Session Summary (Three-Tier Memory) -----------------------------------

async def _update_session_summary(
    old_summary: str,
    rotated_messages: list[dict],
    client: anthropic.AsyncAnthropic,
) -> str:
    """Background Haiku call to update the rolling session summary."""
    prompt = f"""Update this conversation summary to include the new messages.

Current summary: {old_summary or '(start of conversation)'}

New messages to incorporate:
{chr(10).join(f'{m["role"]}: {m["content"][:200]}' for m in rotated_messages)}

Write an updated summary in 2-4 sentences capturing the key topics, decisions, and context. Be concise."""

    # Prefer Claude but fall back to the active provider when no Anthropic key is
    # configured, so the rolling summary keeps working under a non-Claude brain.
    result = await summarize(prompt, max_tokens=200)
    return result.strip() if result else old_summary


# -- WebSocket Voice Handler -----------------------------------------------

@app.websocket("/ws/voice")
async def voice_handler(ws: WebSocket):
    """
    WebSocket protocol:

    Client -> Server:
        {"type": "transcript", "text": "...", "isFinal": true}

    Server -> Client:
        {"type": "audio", "data": "<base64 mp3>", "text": "spoken text"}
        {"type": "status", "state": "thinking"|"speaking"|"idle"|"working"}
        {"type": "task_spawned", "task_id": "...", "prompt": "..."}
        {"type": "task_complete", "task_id": "...", "summary": "..."}
    """
    await ws.accept()
    task_manager.register_websocket(ws)
    history: list[dict] = []
    work_session = WorkSession()
    planner = TaskPlanner()

    # Request sequencing — lets the client discard a stale reply after barge-in
    _current_response_id = 0

    # Audio collision prevention — track when user last spoke
    voice_state = {"last_user_time": 0.0}

    # Self-awareness — track last spoken response to avoid repetition
    last_jarvis_response = ""

    # Three-tier conversation memory
    session_buffer: list[dict] = []  # ALL messages, never truncated
    session_summary: str = ""  # Rolling summary of older conversation
    summary_update_pending: bool = False
    messages_since_last_summary: int = 0

    log.info("Voice WebSocket connected")

    try:
        # ── Greeting — always start in conversation mode ──
        now = datetime.now()
        hour = now.hour
        if hour < 12:
            greeting = "Good morning, sir."
        elif hour < 17:
            greeting = "Good afternoon, sir."
        else:
            greeting = "Good evening, sir."

        global _last_greeting_time
        should_greet = (time.time() - _last_greeting_time) > 60

        if should_greet:
            _last_greeting_time = time.time()

            async def _send_greeting():
                try:
                    audio_bytes = await synthesize_speech(greeting)
                    if audio_bytes:
                        encoded = base64.b64encode(audio_bytes).decode()
                        await ws.send_json({"type": "status", "state": "speaking"})
                        await ws.send_json({"type": "audio", "data": encoded, "text": greeting})
                        history.append({"role": "assistant", "content": greeting})
                        log.info(f"JARVIS: {greeting}")
                        await ws.send_json({"type": "status", "state": "idle"})
                except Exception as e:
                    log.warning(f"Greeting failed: {e}")

            asyncio.create_task(_send_greeting())

        try:
            await ws.send_json({"type": "status", "state": "idle"})
        except Exception:
            return  # WebSocket already gone

        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # ── Fix-self: activate work mode in JARVIS repo ──
            if msg.get("type") == "fix_self":
                jarvis_dir = str(Path(__file__).parent)
                await work_session.start(jarvis_dir)
                response_text = "Work mode active in my own repo, sir. Tell me what needs fixing."
                tts = strip_markdown_for_tts(response_text)
                await ws.send_json({"type": "status", "state": "speaking"})
                audio = await synthesize_speech(tts)
                if audio:
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": response_text})
                else:
                    await ws.send_json({"type": "text", "text": response_text})
                    await ws.send_json({"type": "status", "state": "idle"})
                continue

            if msg.get("type") != "transcript" or not msg.get("isFinal"):
                continue

            user_text = apply_speech_corrections(msg.get("text", "").strip())
            if not user_text:
                continue

            # Track this request so the client can discard a stale reply if the
            # user barges in with a newer utterance before this one finishes.
            _current_response_id += 1
            req_id = msg.get("id", _current_response_id)

            voice_state["last_user_time"] = time.time()
            log.info(f"User: {user_text}")
            await ws.send_json({"type": "status", "state": "thinking"})

            # Lazy project scan on first message
            global cached_projects
            if not cached_projects:
                try:
                    # Run in executor since scan_projects does sync file I/O
                    loop = asyncio.get_event_loop()
                    cached_projects = await asyncio.wait_for(
                        loop.run_in_executor(None, _scan_projects_sync),
                        timeout=3
                    )
                    log.info(f"Scanned {len(cached_projects)} projects")
                except Exception:
                    cached_projects = []

            try:
                # ── CHECK FOR MODE SWITCHES ──
                t_lower = user_text.lower()

                # ── PLANNING MODE: answering clarifying questions ──
                if planner.is_planning:
                    # Check for bypass
                    if any(p in t_lower for p in BYPASS_PHRASES):
                        plan = planner.active_plan
                        if plan:
                            plan.skipped = True
                            for q in plan.pending_questions[plan.current_question_index:]:
                                if q.get("default") is not None and q["key"] not in plan.answers:
                                    plan.answers[q["key"]] = q["default"]
                        prompt = await planner.build_prompt()
                        name = _generate_project_name(prompt)
                        path = str(Path.home() / "Desktop" / name)
                        os.makedirs(path, exist_ok=True)
                        Path(path, "CLAUDE.md").write_text(prompt)
                        did = dispatch_registry.register(name, path, prompt[:200])
                        asyncio.create_task(_execute_prompt_project(name, prompt, work_session, ws, dispatch_id=did, history=history, voice_state=voice_state))
                        planner.reset()
                        response_text = "Building it now, sir."
                    elif planner.active_plan and planner.active_plan.confirmed is False and planner.active_plan.current_question_index >= len(planner.active_plan.pending_questions):
                        # Confirmation phase
                        result = await planner.handle_confirmation(user_text)
                        if result["confirmed"]:
                            prompt = await planner.build_prompt()
                            name = _generate_project_name(prompt)
                            path = str(Path.home() / "Desktop" / name)
                            os.makedirs(path, exist_ok=True)
                            Path(path, "CLAUDE.md").write_text(prompt)
                            did = dispatch_registry.register(name, path, prompt[:200])
                            asyncio.create_task(_execute_prompt_project(name, prompt, work_session, ws, dispatch_id=did, history=history, voice_state=voice_state))
                            planner.reset()
                            response_text = "On it, sir."
                        elif result["cancelled"]:
                            planner.reset()
                            response_text = "Cancelled, sir."
                        else:
                            response_text = result.get("modification_question", "How shall I adjust the plan, sir?")
                    else:
                        result = await planner.process_answer(user_text, cached_projects)
                        if result["plan_complete"]:
                            response_text = result.get("confirmation_summary", "Ready to build. Shall I proceed, sir?")
                        else:
                            response_text = result.get("next_question", "What else, sir?")

                elif any(w in t_lower for w in ["quit work mode", "exit work mode", "go back to chat", "regular mode", "stop working"]):
                    if work_session.active:
                        await work_session.stop()
                        response_text = "Back to conversation mode, sir."
                    else:
                        response_text = "Already in conversation mode, sir."

                # ── WORK MODE: speech → claude -p → Haiku summary → JARVIS voice ──
                elif work_session.active:
                    if is_casual_question(user_text):
                        # Quick chat — bypass claude -p, use Haiku
                        response_text = await generate_response(
                            user_text, anthropic_client, task_manager,
                            cached_projects, history,
                            last_response=last_jarvis_response,
                            session_summary=session_summary,
                        )
                    else:
                        # Send to claude -p (full power)
                        await ws.send_json({"type": "status", "state": "working"})
                        log.info(f"Work mode → claude -p: {user_text[:80]}")

                        full_response = await work_session.send(user_text)

                        # Detect if Claude Code is stalling (asking questions instead of building)
                        if full_response and anthropic_client:
                            stall_words = ["which option", "would you prefer", "would you like me to",
                                           "before I proceed", "before proceeding", "should I",
                                           "do you want me to", "let me know", "please confirm",
                                           "which approach", "what would you"]
                            is_stalling = any(w in full_response.lower() for w in stall_words)
                            if is_stalling and work_session._message_count >= 2:
                                # Claude Code keeps asking — push it to build
                                log.info("Claude Code stalling — pushing to build")
                                push_response = await work_session.send(
                                    "Stop asking questions. Use your best judgment and start building now. "
                                    "Write the actual code files. Go with the simplest reasonable approach."
                                )
                                if push_response:
                                    full_response = push_response

                        # Auto-open any localhost URLs Claude Code mentions
                        import re as _re
                        localhost_match = _re.search(r'https?://localhost:\d+', full_response or "")
                        if localhost_match:
                            asyncio.create_task(_execute_browse(localhost_match.group(0)))
                            log.info(f"Auto-opening {localhost_match.group(0)}")

                        # Always summarize work mode responses via Haiku
                        if full_response and anthropic_client:
                            try:
                                summary = await anthropic_client.messages.create(
                                    model="claude-haiku-4-5-20251001",
                                    max_tokens=100,
                                    system=(
                                        f"You are JARVIS reporting to the user ({USER_NAME}). Summarize what happened in 1-2 sentences. "
                                        "Speak in first person — 'I built', 'I found', 'I set up'. "
                                        "You are talking TO THE USER, not to a coding tool. "
                                        "NEVER give instructions like 'go ahead and build' or 'set up the frontend' — those are NOT for the user. "
                                        "NEVER say 'Claude Code'. NEVER output [ACTION:...] tags. "
                                        "NEVER read out URLs. No markdown. British precision."
                                    ),
                                    messages=[{"role": "user", "content": f"Claude Code said:\n{full_response[:2000]}"}],
                                )
                                response_text = summary.content[0].text
                            except Exception:
                                response_text = full_response[:200]
                        else:
                            response_text = full_response

                # ── CHAT MODE: fast keyword detection + Haiku ──
                else:
                    action = detect_action_fast(user_text)

                    if action:
                        if action["action"] == "open_terminal":
                            response_text = await handle_open_terminal()
                        elif action["action"] == "show_recent":
                            response_text = await handle_show_recent()
                        elif action["action"] == "describe_screen":
                            response_text = "Taking a look now, sir."
                            asyncio.create_task(_lookup_and_report("screen", _do_screen_lookup, ws, history=history, voice_state=voice_state))
                        elif action["action"] == "check_calendar":
                            response_text = "Checking your calendar now, sir."
                            asyncio.create_task(_lookup_and_report("calendar", _do_calendar_lookup, ws, history=history, voice_state=voice_state))
                        elif action["action"] == "check_mail":
                            response_text = "Checking your inbox now, sir."
                            asyncio.create_task(_lookup_and_report("mail", _do_mail_lookup, ws, history=history, voice_state=voice_state))
                        elif action["action"] == "check_dispatch":
                            recent = dispatch_registry.get_most_recent()
                            if not recent:
                                response_text = "No recent builds on record, sir."
                            else:
                                name = recent["project_name"]
                                status = recent["status"]
                                if status == "building" or status == "pending":
                                    elapsed = int(time.time() - recent["updated_at"])
                                    response_text = f"Still working on {name}, sir. Been at it for {elapsed} seconds."
                                elif status == "completed":
                                    response_text = recent.get("summary") or f"{name} is complete, sir."
                                elif status in ("failed", "timeout"):
                                    response_text = f"{name} ran into problems, sir."
                                else:
                                    response_text = f"{name} is {status}, sir."
                        elif action["action"] == "check_tasks":
                            tasks = get_open_tasks()
                            response_text = format_tasks_for_voice(tasks)
                        elif action["action"] == "check_usage":
                            response_text = get_usage_summary()
                        else:
                            response_text = "Understood, sir."
                    else:
                        if not anthropic_client:
                            response_text = "API key not configured."
                        else:
                            response_text = await generate_response(
                                user_text, anthropic_client, task_manager,
                                cached_projects, history,
                                last_response=last_jarvis_response,
                                session_summary=session_summary,
                            )

                            # Check for action tags embedded in LLM response
                            clean_response, embedded_action = extract_action(response_text)
                            if embedded_action:
                                log.info(f"LLM embedded action: {embedded_action}")
                                response_text = clean_response
                                # Ensure there's always something to speak
                                if not response_text.strip():
                                    action_type = embedded_action["action"]
                                    if action_type == "prompt_project":
                                        proj = embedded_action["target"].split("|||")[0].strip()
                                        response_text = f"Connecting to {proj} now, sir."
                                    elif action_type == "build":
                                        response_text = "On it, sir."
                                    elif action_type == "research":
                                        response_text = "Looking into that now, sir."
                                    else:
                                        response_text = "Right away, sir."

                                if embedded_action["action"] == "build":
                                    # Build in background — JARVIS stays conversational
                                    target = embedded_action["target"]
                                    name = _generate_project_name(target)
                                    path = str(Path.home() / "Desktop" / name)
                                    os.makedirs(path, exist_ok=True)

                                    # Write detailed CLAUDE.md
                                    Path(path, "CLAUDE.md").write_text(
                                        f"# Task\n\n{target}\n\n"
                                        "## Instructions\n"
                                        "- BUILD THIS NOW. Do not ask clarifying questions.\n"
                                        "- Use your best judgment for any design/architecture decisions.\n"
                                        "- Write complete, working code files — not plans or specs.\n"
                                        "- If it's a web app: use React + Vite + Tailwind unless specified otherwise.\n"
                                        "- Make it look polished and professional. Modern UI, clean layout.\n"
                                        "- Ensure it runs with a single command (npm run dev or similar).\n"
                                        "- If you reference a real product's UI (e.g. 'Zillow clone'), match their actual layout and features closely.\n"
                                        "- Use realistic mock data, not placeholder Lorem Ipsum.\n"
                                        "- After building, start the dev server and verify the app loads without errors.\n"
                                        "- IMPORTANT: Your LAST line of output MUST be exactly: RUNNING_AT=http://localhost:PORT (the actual port the dev server is using)\n"
                                    )

                                    # Register and dispatch
                                    did = dispatch_registry.register(name, path, target)
                                    asyncio.create_task(
                                        _execute_prompt_project(name, target, work_session, ws, dispatch_id=did, history=history, voice_state=voice_state)
                                    )
                                elif embedded_action["action"] == "browse":
                                    asyncio.create_task(_execute_browse(embedded_action["target"]))
                                elif embedded_action["action"] in ("open_app", "open_path", "set_volume", "media", "lock_screen", "clipboard", "screenshot"):
                                    asyncio.create_task(_execute_desktop_control(embedded_action["action"], embedded_action["target"]))
                                elif embedded_action["action"] == "research":
                                    # Research enters work mode too
                                    name = _generate_project_name(embedded_action["target"])
                                    path = str(Path.home() / "Desktop" / name)
                                    os.makedirs(path, exist_ok=True)
                                    await work_session.start(path)
                                    asyncio.create_task(
                                        self_work_and_notify(work_session, embedded_action["target"], ws)
                                    )
                                elif embedded_action["action"] == "open_terminal":
                                    asyncio.create_task(_execute_open_terminal())
                                elif embedded_action["action"] == "prompt_project":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        proj_name, _, prompt = target.partition("|||")
                                        proj_name = proj_name.strip()
                                        prompt = prompt.strip()
                                        # Check for recent completed dispatch before re-dispatching
                                        recent = dispatch_registry.get_recent_for_project(proj_name)
                                        if recent and recent.get("summary"):
                                            log.info(f"Using recent dispatch result for {proj_name} instead of re-dispatching")
                                            response_text = recent["summary"]
                                            history.append({"role": "assistant", "content": f"[Previous dispatch result for {proj_name}]: {recent['summary']}"})
                                        else:
                                            asyncio.create_task(
                                                _execute_prompt_project(proj_name, prompt, work_session, ws, history=history, voice_state=voice_state)
                                            )
                                    else:
                                        log.warning(f"PROMPT_PROJECT missing ||| delimiter: {target}")
                                elif embedded_action["action"] == "add_task":
                                    target = embedded_action["target"]
                                    parts = target.split("|||")
                                    if len(parts) >= 2:
                                        priority = parts[0].strip() or "medium"
                                        title = parts[1].strip()
                                        desc = parts[2].strip() if len(parts) > 2 else ""
                                        due = parts[3].strip() if len(parts) > 3 else ""
                                        create_task(title=title, description=desc, priority=priority, due_date=due)
                                        log.info(f"Task created: {title}")
                                elif embedded_action["action"] == "add_note":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        topic, _, content = target.partition("|||")
                                        create_note(content=content.strip(), topic=topic.strip())
                                    else:
                                        create_note(content=target)
                                    log.info(f"Note created")
                                elif embedded_action["action"] == "complete_task":
                                    try:
                                        task_id = int(embedded_action["target"].strip())
                                        complete_task(task_id)
                                        log.info(f"Task {task_id} completed")
                                    except ValueError:
                                        pass
                                elif embedded_action["action"] == "remember":
                                    remember(embedded_action["target"].strip(), mem_type="fact", importance=7)
                                    log.info(f"Memory stored: {embedded_action['target'][:60]}")
                                elif embedded_action["action"] == "create_note":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        title, _, body = target.partition("|||")
                                        asyncio.create_task(create_apple_note(title.strip(), body.strip()))
                                        log.info(f"Apple Note created: {title.strip()}")
                                    else:
                                        asyncio.create_task(create_apple_note("JARVIS Note", target))
                                elif embedded_action["action"] == "screen":
                                    asyncio.create_task(_lookup_and_report("screen", _do_screen_lookup, ws, history=history, voice_state=voice_state))
                                elif embedded_action["action"] == "read_note":
                                    # Read note in background and report back
                                    async def _read_and_report(search_term, _ws):
                                        note = await read_note(search_term)
                                        if note:
                                            msg = f"Sir, your note '{note['title']}' says: {note['body'][:200]}"
                                        else:
                                            msg = f"Couldn't find a note matching '{search_term}', sir."
                                        audio = await synthesize_speech(strip_markdown_for_tts(msg))
                                        if audio and _ws:
                                            try:
                                                await _ws.send_json({"type": "status", "state": "speaking"})
                                                await _ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                                            except Exception:
                                                pass
                                    asyncio.create_task(_read_and_report(embedded_action["target"].strip(), ws))
                                elif embedded_action["action"] == "profile":
                                    # Store a piece of the user profile learned during onboarding
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        key, _, value = target.partition("|||")
                                        key = key.strip().lower().replace(" ", "_")
                                        value = value.strip()
                                        if key and value:
                                            onboarding_system.set_profile(key, value)
                                            onboarding_system.mark_turn()
                                            # Mirror the user's name into the .env so the rest of JARVIS uses it
                                            if key == "name":
                                                try:
                                                    _write_env_key("USER_NAME", value)
                                                    _set_user_name(value)
                                                except Exception:
                                                    pass
                                            log.info(f"Profile set: {key}={value[:40]}")
                                elif embedded_action["action"] == "recommend_skills":
                                    goal = embedded_action["target"].strip()
                                    recs = skills_system.recommend_skills(goal, limit=6)
                                    for s in recs[:4]:
                                        skills_system.set_skill_enabled(s["slug"], True)
                                    # Also recommend matching MCP tools from the profile's tools
                                    tools_text = onboarding_system.get_profile().get("tools", "") + " " + goal
                                    for srv in mcp_registry.recommend_servers(tools_text, limit=3):
                                        log.info(f"Onboarding suggests MCP: {srv['name']}")
                                    log.info(f"Recommended/enabled skills for goal '{goal[:40]}': {[s['slug'] for s in recs[:4]]}")
                                elif embedded_action["action"] == "onboard_done":
                                    onboarding_system.complete()
                                    log.info("Onboarding marked complete")
                                elif embedded_action["action"] == "run_skill":
                                    # [ACTION:RUN_SKILL] slug ||| optional json params
                                    target = embedded_action["target"]
                                    slug, _, raw = target.partition("|||")
                                    slug = slug.strip()
                                    params = {}
                                    if raw.strip():
                                        try:
                                            params = json.loads(raw.strip())
                                        except Exception:
                                            params = {"description": raw.strip()}
                                    result = skills_system.run_skill(slug, params)
                                    action_log.record_action(
                                        "run_skill",
                                        f"Run skill {slug}",
                                        status="failed" if result.get("error") else "completed",
                                        risk="low",
                                        target=slug,
                                        details={"params": params},
                                        result=result,
                                    )
                                    if result.get("error"):
                                        log.warning(f"RUN_SKILL {slug} failed: {result['error']}")
                                    else:
                                        log.info(f"RUN_SKILL {slug}: {result.get('summary')}")
                                elif embedded_action["action"] == "control_center":
                                    parts = [p.strip() for p in embedded_action["target"].split("|||", 2)]
                                    title = parts[0] if parts and parts[0] else "JARVIS update"
                                    body = parts[1] if len(parts) > 1 else ""
                                    category = parts[2] if len(parts) > 2 and parts[2] else "jarvis"
                                    try:
                                        await ws.send_json({"type": "control_center", "title": title, "body": body, "category": category})
                                    except Exception:
                                        pass
                                elif embedded_action["action"] == "mcp_call":
                                    # [ACTION:MCP_CALL] server_id ||| tool_name ||| optional json args
                                    parts = [p.strip() for p in embedded_action["target"].split("|||", 2)]
                                    if len(parts) >= 2:
                                        server_id, tool_name = parts[0], parts[1]
                                        args = {}
                                        if len(parts) == 3 and parts[2]:
                                            try:
                                                args = json.loads(parts[2])
                                            except Exception:
                                                args = {"input": parts[2]}
                                        risk = action_log.risk_for_tool(server_id, tool_name, args)
                                        details = {"server_id": server_id, "tool": tool_name, "arguments": args}
                                        title = f"Call {server_id}.{tool_name}"
                                        if action_log.requires_confirmation(risk):
                                            pending = action_log.create_pending("mcp_tool_call", title, risk=risk, target=server_id, details=details)
                                            log.info(f"MCP call queued for confirmation: {pending['id']} {title}")
                                            try:
                                                await ws.send_json({"type": "action_pending", "action": pending})
                                            except Exception:
                                                pass
                                        else:
                                            try:
                                                result = await _execute_mcp_tool_call(server_id, tool_name, args)
                                                action_log.record_action("mcp_tool_call", title, risk=risk, target=server_id, details=details, result=result)
                                                log.info(f"MCP call completed: {title}")
                                            except Exception as e:
                                                action_log.record_action("mcp_tool_call", title, status="failed", risk=risk, target=server_id, details=details, result={"error": str(e)})
                                                log.warning(f"MCP call failed: {e}")

                # Update history
                history.append({"role": "user", "content": user_text})
                history.append({"role": "assistant", "content": response_text})

                # Three-tier memory: also track in session buffer
                session_buffer.append({"role": "user", "content": user_text})
                session_buffer.append({"role": "assistant", "content": response_text})

                # Check if rolling summary needs updating
                messages_since_last_summary += 1
                if messages_since_last_summary >= 5 and len(history) > 20 and not summary_update_pending:
                    summary_update_pending = True
                    messages_since_last_summary = 0
                    # Get messages that are about to be rotated out
                    rotated = history[:-20] if len(history) > 20 else []
                    if rotated and anthropic_client:
                        async def _do_summary():
                            nonlocal session_summary, summary_update_pending
                            session_summary = await _update_session_summary(
                                session_summary, rotated, anthropic_client
                            )
                            summary_update_pending = False
                        asyncio.create_task(_do_summary())
                    else:
                        summary_update_pending = False

                # Extract memories in background (doesn't block response)
                if anthropic_client and len(user_text) > 15:
                    asyncio.create_task(extract_memories(user_text, response_text, anthropic_client))

                # TTS
                tts = strip_markdown_for_tts(response_text)
                await ws.send_json({"type": "status", "state": "speaking"})
                audio = await synthesize_speech(tts)
                if audio:
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": response_text, "reqId": req_id})
                else:
                    await ws.send_json({"type": "text", "text": response_text, "reqId": req_id})
                    await ws.send_json({"type": "status", "state": "idle"})
                log.info(f"JARVIS: {response_text}")
                last_jarvis_response = response_text

            except Exception as e:
                log.error(f"Error: {e}", exc_info=True)
                try:
                    fallback = "Something went wrong, sir."
                    audio = await synthesize_speech(fallback)
                    if audio:
                        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": fallback})
                    else:
                        await ws.send_json({"type": "audio", "data": "", "text": fallback})
                    # Let client's audioPlayer.onFinished handle idle transition
                except Exception:
                    pass

    except WebSocketDisconnect:
        log.info("Voice WebSocket disconnected")
    except Exception as e:
        log.error(f"WebSocket error: {e}", exc_info=True)
    finally:
        task_manager.unregister_websocket(ws)


# ---------------------------------------------------------------------------
# Settings / Configuration endpoints
# ---------------------------------------------------------------------------

def _env_file_path() -> Path:
    return Path(__file__).parent / ".env"

def _env_example_path() -> Path:
    return Path(__file__).parent / ".env.example"

def _read_env() -> tuple[list[str], dict[str, str]]:
    """Read .env file. Returns (raw_lines, parsed_dict). Creates from .env.example if missing."""
    path = _env_file_path()
    if not path.exists():
        example = _env_example_path()
        if example.exists():
            import shutil as _shutil
            _shutil.copy2(str(example), str(path))
        else:
            path.write_text("")
    lines = path.read_text().splitlines()
    parsed: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, v = stripped.partition("=")
            parsed[k.strip()] = v.strip().strip('"').strip("'")
    return lines, parsed

def _write_env_key(key: str, value: str) -> None:
    """Update a single key in .env, preserving comments and order."""
    lines, _ = _read_env()
    found = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, _ = stripped.partition("=")
            if k.strip() == key:
                new_lines.append(f"{key}={value}")
                found = True
                continue
        new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}")
    _env_file_path().write_text("\n".join(new_lines) + "\n")
    os.environ[key] = value

class KeyUpdate(BaseModel):
    key_name: str
    key_value: str

class KeyTest(BaseModel):
    key_value: str | None = None

class ProviderTest(BaseModel):
    provider: str
    key_value: str | None = None

class ActiveUpdate(BaseModel):
    llm_provider: str | None = None
    llm_model: str | None = None
    tts_provider: str | None = None

class PreferencesUpdate(BaseModel):
    user_name: str = ""
    honorific: str = "sir"
    calendar_accounts: str = "auto"
    interface_language: str = "en"
    response_language: str = "en"
    personality_preset: str = "stark"
    personality_brief: str = ""
    humor_level: str = "balanced"
    formality_level: str = "butler"
    proactive_mode: str = "smart"


def _personalization_prompt() -> str:
    """Runtime personality controls saved from Settings → Personalization."""
    _, env = _read_env()
    language_names = {
        "en": "English",
        "pl": "Polish",
        "es": "Spanish",
        "de": "German",
        "fr": "French",
        "it": "Italian",
        "pt": "Portuguese",
        "uk": "Ukrainian",
    }
    preset = env.get("JARVIS_PERSONALITY_PRESET", "stark")
    preset_lines = {
        "stark": "Default: cinematic Stark-style JARVIS — polished British butler, loyal, calm, highly capable, with understated dry wit.",
        "executive": "Executive operator — concise, strategic, numbers-first, ruthless about priorities.",
        "coach": "Supportive coach — energetic, encouraging, habit-building, but still elegant.",
        "engineer": "Senior engineer — precise, technical, skeptical of assumptions, prefers tests and evidence.",
        "creative": "Creative director — visual, bold, idea-rich, brand-aware, and playful.",
    }.get(preset, "Cinematic Stark-style JARVIS with elegant service and dry wit.")
    response_language = env.get("JARVIS_RESPONSE_LANGUAGE", "en")
    brief = env.get("JARVIS_PERSONALITY_BRIEF", "").strip()
    speech_language = env.get("JARVIS_SPEECH_LANGUAGE", "browser-selected")
    lines = [
        "RUNTIME PERSONALIZATION:",
        f"- Speak to the user in {language_names.get(response_language, response_language)} unless the user explicitly asks for another language.",
        f"- Personality preset: {preset_lines}",
        f"- Humor level: {env.get('JARVIS_HUMOR_LEVEL', 'balanced')} — keep it dry, never clownish.",
        f"- Formality level: {env.get('JARVIS_FORMALITY_LEVEL', 'butler')}.",
        f"- Proactive mode: {env.get('JARVIS_PROACTIVE_MODE', 'smart')} — take reasonable initiative without being intrusive.",
        f"- Speech capture: browser-selected language is {speech_language}; keep replies easy to pronounce and pleasant when synthesized.",
    ]
    if brief:
        lines.append(f"- Custom personality directive from the user: {brief[:1200]}")
    return "\n".join(lines)

def _rebuild_anthropic_client():
    """Recreate the Anthropic client from the current env so a freshly-saved key
    takes effect without a server restart."""
    global anthropic_client
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    anthropic_client = anthropic.AsyncAnthropic(api_key=key) if key else None


@app.post("/api/settings/keys")
async def api_settings_keys(body: KeyUpdate):
    allowed = (
        provider_env_keys()
        | {
            "FISH_VOICE_ID",
            "USER_NAME",
            "HONORIFIC",
            "CALENDAR_ACCOUNTS",
            "JARVIS_UI_LANGUAGE",
            "JARVIS_RESPONSE_LANGUAGE",
            "JARVIS_PERSONALITY_PRESET",
            "JARVIS_PERSONALITY_BRIEF",
            "JARVIS_HUMOR_LEVEL",
            "JARVIS_FORMALITY_LEVEL",
            "JARVIS_PROACTIVE_MODE",
            "JARVIS_SPEECH_LANGUAGE",
            "WEATHER_LOCATION_LABEL",
            "WEATHER_LATITUDE",
            "WEATHER_LONGITUDE",
            "WEATHER_UNIT",
        }
        | provider_config.extra_env_keys()
        | mcp_registry.auth_env_keys()
    )
    if body.key_name not in allowed:
        return JSONResponse({"success": False, "error": "Invalid key name"}, status_code=400)
    _write_env_key(body.key_name, body.key_value)
    if body.key_name == "ANTHROPIC_API_KEY":
        _rebuild_anthropic_client()
    return {"success": True}


@app.post("/api/settings/active")
async def api_settings_active(body: ActiveUpdate):
    """Persist the active LLM brain / model / voice provider."""
    if body.llm_provider is not None:
        if body.llm_provider not in provider_config.LLM_PROVIDERS:
            return JSONResponse({"success": False, "error": "Unknown LLM provider"}, status_code=400)
        _write_env_key("JARVIS_LLM_PROVIDER", body.llm_provider)
    if body.llm_model is not None and body.llm_provider:
        _write_env_key(provider_config.model_env_key(body.llm_provider), body.llm_model)
    if body.tts_provider is not None:
        if body.tts_provider not in provider_config.TTS_PROVIDERS:
            return JSONResponse({"success": False, "error": "Unknown TTS provider"}, status_code=400)
        _write_env_key("JARVIS_TTS_PROVIDER", body.tts_provider)
    return {"success": True, "llm": provider_config.llm_status(), "tts": provider_config.tts_status()}


@app.post("/api/settings/test-provider")
async def api_test_provider(body: ProviderTest):
    """Connectivity check for any LLM or TTS provider."""
    if body.provider in provider_config.TTS_PROVIDERS:
        return await provider_tts.test_provider(body.provider, body.key_value or None)
    if body.provider in provider_config.LLM_PROVIDERS:
        return await provider_llm.test_provider(body.provider, body.key_value or None)
    return {"valid": False, "error": "Unknown provider"}


@app.get("/api/settings/ollama-models")
async def api_ollama_models():
    models, error = await provider_llm.list_ollama_models()
    return {"models": models, "error": error}

@app.post("/api/settings/test-anthropic")
async def api_test_anthropic(body: KeyTest):
    key = body.key_value or os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return {"valid": False, "error": "No key provided"}
    try:
        client = anthropic.AsyncAnthropic(api_key=key)
        await client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=10, messages=[{"role": "user", "content": "Hi"}])
        return {"valid": True}
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}

@app.post("/api/settings/test-fish")
async def api_test_fish(body: KeyTest):
    key = body.key_value or os.getenv("FISH_API_KEY", "")
    if not key:
        return {"valid": False, "error": "No key provided"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.fish.audio/v1/tts",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"text": "test", "reference_id": FISH_VOICE_ID},
            )
            if resp.status_code in (200, 201):
                return {"valid": True}
            elif resp.status_code == 401:
                return {"valid": False, "error": "Invalid API key"}
            else:
                return {"valid": False, "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}

@app.get("/api/settings/status")
async def api_settings_status():
    import shutil as _shutil
    _, env_dict = _read_env()
    claude_installed = _shutil.which("claude") is not None
    calendar_ok = mail_ok = notes_ok = False
    try: await get_todays_events(); calendar_ok = True
    except Exception: pass
    try: await get_unread_count(); mail_ok = True
    except Exception: pass
    try: await get_recent_notes(count=1); notes_ok = True
    except Exception: pass
    memory_count = task_count = 0
    try: memory_count = len(get_important_memories(limit=9999))
    except Exception: pass
    try: task_count = len(get_open_tasks())
    except Exception: pass
    return {
        "claude_code_installed": claude_installed,
        "calendar_accessible": calendar_ok,
        "mail_accessible": mail_ok,
        "notes_accessible": notes_ok,
        "memory_count": memory_count,
        "task_count": task_count,
        "server_port": 8340,
        "uptime_seconds": int(time.time() - _session_start),
        "env_keys_set": {
            "anthropic": is_configured(env_dict.get("ANTHROPIC_API_KEY", "")),
            "fish_audio": is_configured(env_dict.get("FISH_API_KEY", "")),
            "fish_voice_id": bool(env_dict.get("FISH_VOICE_ID", "").strip()),
            "user_name": env_dict.get("USER_NAME", ""),
            "interface_language": env_dict.get("JARVIS_UI_LANGUAGE", "en"),
            "response_language": env_dict.get("JARVIS_RESPONSE_LANGUAGE", "en"),
        },
        "providers": providers_for_status(env_dict),
        "llm": provider_config.llm_status(),
        "tts": provider_config.tts_status(),
        "skills": skills_for_status(),
        "skill_counts": skills_system.counts(),
        "mcp_connected": len(mcp_registry.connected_servers()),
        "onboarding": onboarding_system.get_state(),
        "platform": platform.system(),
    }

@app.get("/api/settings/providers")
async def api_settings_providers():
    _, env_dict = _read_env()
    return {"providers": providers_for_status(env_dict)}


# ---------------------------------------------------------------------------
# Skills API
# ---------------------------------------------------------------------------

@app.get("/api/skills")
async def api_skills():
    """Full skill catalog with categories and counts."""
    return {
        "skills": skills_system.list_skills(),
        "categories": skills_system.categories_summary(),
        "counts": skills_system.counts(),
        "packs": skills_for_status(),
    }


@app.get("/api/skills/search")
async def api_skills_search(q: str = ""):
    return {"skills": skills_system.search_skills(q)}


class SkillToggle(BaseModel):
    enabled: bool


@app.post("/api/skills/{slug}/toggle")
async def api_skill_toggle(slug: str, body: SkillToggle):
    ok = skills_system.set_skill_enabled(slug, body.enabled)
    if not ok:
        return JSONResponse({"error": "Unknown skill"}, status_code=404)
    return {"success": True, "slug": slug, "enabled": body.enabled, "counts": skills_system.counts()}


class SkillRun(BaseModel):
    params: dict = Field(default_factory=dict)


@app.post("/api/skills/{slug}/run")
async def api_skill_run(slug: str, body: SkillRun):
    skill = skills_system.get_skill(slug)
    if not skill:
        return JSONResponse({"error": "Unknown skill"}, status_code=404)
    if not skill["executable"]:
        return JSONResponse({"error": "Skill is not executable"}, status_code=400)
    result = skills_system.run_skill(slug, body.params)
    action_log.record_action(
        "run_skill",
        f"Run skill {slug}",
        status="failed" if result.get("error") else "completed",
        risk="low",
        target=slug,
        details={"params": body.params},
        result=result,
    )
    return result



# ---------------------------------------------------------------------------
# Artifacts and action log API
# ---------------------------------------------------------------------------

@app.get("/api/artifacts")
async def api_artifacts():
    return {"artifacts": skills_system.list_artifacts()}


@app.get("/api/artifacts/{name}")
async def api_artifact_download(name: str):
    safe = Path(name).name
    path = skills_system.ARTIFACTS_DIR / safe
    if not path.exists() or not path.is_file():
        return JSONResponse({"error": "Artifact not found"}, status_code=404)
    return FileResponse(str(path), filename=safe)


@app.get("/api/artifacts/{name}/preview")
async def api_artifact_preview(name: str):
    result = skills_system.read_artifact(name)
    if result.get("error"):
        return JSONResponse(result, status_code=404)
    return result


@app.get("/api/action-log")
async def api_action_log(limit: int = 50, status: str | None = None):
    return {"actions": action_log.list_actions(limit=limit, status=status)}


@app.post("/api/action-log/{action_id}/cancel")
async def api_action_cancel(action_id: int):
    action = action_log.mark_cancelled(action_id)
    if not action:
        return JSONResponse({"error": "Action not found"}, status_code=404)
    return action

# ---------------------------------------------------------------------------
# MCP API
# ---------------------------------------------------------------------------

@app.get("/api/mcp")
async def api_mcp_list():
    return {"servers": mcp_registry.list_servers()}


class McpConnect(BaseModel):
    config: dict = Field(default_factory=dict)


@app.post("/api/mcp/{server_id}/connect")
async def api_mcp_connect(server_id: str, body: McpConnect):
    result = mcp_registry.connect(server_id, body.config)
    if result.get("error"):
        return JSONResponse(result, status_code=404)
    action_log.record_action("mcp_connect", f"Connect MCP server {server_id}", target=server_id, details={"config": body.config}, result=result)
    return result


@app.post("/api/mcp/{server_id}/disconnect")
async def api_mcp_disconnect(server_id: str):
    result = mcp_registry.disconnect(server_id)
    action_log.record_action("mcp_disconnect", f"Disconnect MCP server {server_id}", target=server_id, result=result)
    return result


@app.get("/api/mcp/{server_id}/tools")
async def api_mcp_tools(server_id: str):
    try:
        return {"tools": await mcp_client.list_tools(server_id)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


class McpToolCall(BaseModel):
    tool: str
    arguments: dict = Field(default_factory=dict)
    confirm: bool = False


async def _execute_mcp_tool_call(server_id: str, tool: str, arguments: dict) -> dict:
    result = await mcp_client.call_tool(server_id, tool, arguments)
    return {"server_id": server_id, "tool": tool, "result": result}


@app.post("/api/mcp/{server_id}/call")
async def api_mcp_call(server_id: str, body: McpToolCall):
    risk = action_log.risk_for_tool(server_id, body.tool, body.arguments)
    title = f"Call {server_id}.{body.tool}"
    details = {"server_id": server_id, "tool": body.tool, "arguments": body.arguments}
    if action_log.requires_confirmation(risk) and not body.confirm:
        pending = action_log.create_pending("mcp_tool_call", title, risk=risk, target=server_id, details=details)
        return {"requires_confirmation": True, "pending_action": pending}
    try:
        result = await _execute_mcp_tool_call(server_id, body.tool, body.arguments)
        logged = action_log.record_action("mcp_tool_call", title, risk=risk, target=server_id, details=details, result=result)
        return {"success": True, "action": logged, "result": result}
    except Exception as e:
        action_log.record_action("mcp_tool_call", title, status="failed", risk=risk, target=server_id, details=details, result={"error": str(e)})
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/action-log/{action_id}/confirm")
async def api_action_confirm(action_id: int):
    action = action_log.get_action(action_id)
    if not action:
        return JSONResponse({"error": "Action not found"}, status_code=404)
    if action["status"] != "pending_confirmation":
        return JSONResponse({"error": "Action is not pending confirmation"}, status_code=400)
    if action["action_type"] != "mcp_tool_call":
        return JSONResponse({"error": "Unsupported confirmation type"}, status_code=400)
    details = action.get("details", {})
    try:
        result = await _execute_mcp_tool_call(details["server_id"], details["tool"], details.get("arguments") or {})
        return action_log.mark_completed(action_id, result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


# ---------------------------------------------------------------------------
# Onboarding API
# ---------------------------------------------------------------------------

@app.get("/api/onboarding")
async def api_onboarding_get():
    return onboarding_system.export()


class ProfileUpdate(BaseModel):
    profile: dict = Field(default_factory=dict)


@app.post("/api/onboarding/profile")
async def api_onboarding_profile(body: ProfileUpdate):
    onboarding_system.set_profile_many(body.profile)
    name = body.profile.get("name")
    if name and str(name).strip():
        _write_env_key("USER_NAME", str(name).strip())
        _set_user_name(str(name).strip())
    return onboarding_system.export()


@app.post("/api/onboarding/complete")
async def api_onboarding_complete():
    onboarding_system.complete()
    return onboarding_system.get_state()


@app.post("/api/onboarding/skip")
async def api_onboarding_skip():
    onboarding_system.skip()
    return onboarding_system.get_state()


@app.post("/api/onboarding/reset")
async def api_onboarding_reset():
    onboarding_system.reset()
    return onboarding_system.get_state()

@app.get("/api/settings/preferences")
async def api_get_preferences():
    _, env_dict = _read_env()
    return {
        "user_name": env_dict.get("USER_NAME", ""),
        "honorific": env_dict.get("HONORIFIC", "sir"),
        "calendar_accounts": env_dict.get("CALENDAR_ACCOUNTS", "auto"),
        "interface_language": env_dict.get("JARVIS_UI_LANGUAGE", "en"),
        "response_language": env_dict.get("JARVIS_RESPONSE_LANGUAGE", "en"),
        "personality_preset": env_dict.get("JARVIS_PERSONALITY_PRESET", "stark"),
        "personality_brief": env_dict.get("JARVIS_PERSONALITY_BRIEF", ""),
        "humor_level": env_dict.get("JARVIS_HUMOR_LEVEL", "balanced"),
        "formality_level": env_dict.get("JARVIS_FORMALITY_LEVEL", "butler"),
        "proactive_mode": env_dict.get("JARVIS_PROACTIVE_MODE", "smart"),
    }

@app.post("/api/settings/preferences")
async def api_save_preferences(body: PreferencesUpdate):
    _write_env_key("USER_NAME", body.user_name)
    _write_env_key("HONORIFIC", body.honorific)
    _write_env_key("CALENDAR_ACCOUNTS", body.calendar_accounts)
    _write_env_key("JARVIS_UI_LANGUAGE", body.interface_language)
    _write_env_key("JARVIS_RESPONSE_LANGUAGE", body.response_language)
    _write_env_key("JARVIS_PERSONALITY_PRESET", body.personality_preset)
    _write_env_key("JARVIS_PERSONALITY_BRIEF", body.personality_brief)
    _write_env_key("JARVIS_HUMOR_LEVEL", body.humor_level)
    _write_env_key("JARVIS_FORMALITY_LEVEL", body.formality_level)
    _write_env_key("JARVIS_PROACTIVE_MODE", body.proactive_mode)
    if body.user_name:
        _set_user_name(body.user_name)
    return {"success": True}



# ---------------------------------------------------------------------------
# Voice sample capture API
# ---------------------------------------------------------------------------

VOICE_SAMPLE_DIR = Path(__file__).parent / "data" / "voice_samples"
VOICE_SAMPLE_META = VOICE_SAMPLE_DIR / "samples.json"
VOICE_SAMPLE_MAX_BYTES = 5 * 1024 * 1024
VOICE_SAMPLE_MIME_EXT = {
    "audio/webm": ".webm",
    "audio/webm;codecs=opus": ".webm",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}


def _safe_voice_filename(name: str, mime_type: str) -> str:
    stem = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in Path(name).stem).strip("-_")
    stem = stem[:80] or f"jarvis-voice-sample-{int(time.time())}"
    ext = VOICE_SAMPLE_MIME_EXT.get(mime_type, ".webm")
    return f"{stem}{ext}"


def _read_voice_sample_meta() -> list[dict]:
    if not VOICE_SAMPLE_META.exists():
        return []
    try:
        data = json.loads(VOICE_SAMPLE_META.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_voice_sample_meta(samples: list[dict]) -> None:
    VOICE_SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    VOICE_SAMPLE_META.write_text(json.dumps(samples, indent=2))


@app.get("/api/voice-samples")
async def api_voice_samples():
    VOICE_SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    samples = []
    for item in _read_voice_sample_meta():
        file_path = VOICE_SAMPLE_DIR / item.get("name", "")
        if not file_path.exists():
            continue
        stat = file_path.stat()
        samples.append({
            "name": item.get("name"),
            "mime_type": item.get("mime_type", "audio/webm"),
            "duration_seconds": float(item.get("duration_seconds") or 0),
            "size_bytes": stat.st_size,
            "created_at": float(item.get("created_at") or stat.st_mtime),
            "download_url": f"/api/voice-samples/{item.get('name')}",
        })
    samples.sort(key=lambda x: x["created_at"], reverse=True)
    return {"samples": samples}


@app.post("/api/voice-samples")
async def api_save_voice_sample(body: VoiceSampleRequest):
    if body.mime_type not in VOICE_SAMPLE_MIME_EXT:
        return JSONResponse({"success": False, "error": "Unsupported audio format"}, status_code=400)
    try:
        audio = base64.b64decode(body.data_base64, validate=True)
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid audio payload"}, status_code=400)
    if not audio:
        return JSONResponse({"success": False, "error": "Empty audio sample"}, status_code=400)
    if len(audio) > VOICE_SAMPLE_MAX_BYTES:
        return JSONResponse({"success": False, "error": "Voice sample is larger than 5 MB"}, status_code=400)

    VOICE_SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_voice_filename(body.filename, body.mime_type)
    target = VOICE_SAMPLE_DIR / safe_name
    counter = 2
    while target.exists():
        safe_name = f"{target.stem}-{counter}{target.suffix}"
        target = VOICE_SAMPLE_DIR / safe_name
        counter += 1
    target.write_bytes(audio)

    samples = _read_voice_sample_meta()
    entry = {
        "name": safe_name,
        "mime_type": body.mime_type,
        "duration_seconds": round(float(body.duration_seconds), 2),
        "size_bytes": len(audio),
        "created_at": time.time(),
    }
    samples.insert(0, entry)
    _write_voice_sample_meta(samples[:25])
    return {"success": True, "sample": {**entry, "download_url": f"/api/voice-samples/{safe_name}"}}


@app.get("/api/voice-samples/{name}")
async def api_download_voice_sample(name: str):
    safe_name = _safe_voice_filename(name, "audio/webm") if Path(name).suffix == "" else Path(name).name
    target = VOICE_SAMPLE_DIR / safe_name
    if not target.exists() or not target.is_file():
        return JSONResponse({"error": "Voice sample not found"}, status_code=404)
    return FileResponse(target, media_type="application/octet-stream", filename=target.name)


# ---------------------------------------------------------------------------
# Memory API
# ---------------------------------------------------------------------------

@app.get("/api/memories")
async def api_memories(limit: int = 100, offset: int = 0, type: str | None = None):
    """List memories with pagination and optional type filter."""
    from memory import get_all_memories, memory_stats
    return {
        "memories": get_all_memories(limit=limit, offset=offset, mem_type=type),
        "stats": memory_stats(),
    }


@app.get("/api/memories/stats")
async def api_memory_stats():
    from memory import memory_stats
    return memory_stats()


@app.get("/api/memories/search")
async def api_memory_search(q: str = ""):
    if not q.strip():
        return {"memories": []}
    return {"memories": recall(q, limit=20)}


class MemoryCreate(BaseModel):
    content: str
    type: str = "fact"
    importance: int = 5
    source: str = "manual"


@app.post("/api/memories")
async def api_memory_create(body: MemoryCreate):
    mem_id = remember(body.content, mem_type=body.type, source=body.source, importance=body.importance)
    return {"id": mem_id, "success": True}


@app.delete("/api/memories/{mem_id}")
async def api_memory_delete(mem_id: int):
    from memory import delete_memory
    ok = delete_memory(mem_id)
    if not ok:
        return JSONResponse({"error": "Memory not found"}, status_code=404)
    return {"success": True, "id": mem_id}


# ---------------------------------------------------------------------------
# Task-Memory API (JARVIS internal task system, distinct from Claude Code tasks)
# ---------------------------------------------------------------------------

@app.get("/api/tasks-memory")
async def api_tasks_memory(project: str | None = None):
    tasks = get_open_tasks(project=project)
    return {"tasks": tasks}


class TaskMemoryCreate(BaseModel):
    title: str
    description: str = ""
    priority: str = "medium"
    due_date: str = ""
    due_time: str = ""
    project: str = ""
    tags: list[str] = Field(default_factory=list)


@app.post("/api/tasks-memory")
async def api_task_memory_create(body: TaskMemoryCreate):
    task_id = create_task(
        title=body.title, description=body.description,
        priority=body.priority, due_date=body.due_date,
        due_time=body.due_time, project=body.project, tags=body.tags,
    )
    return {"id": task_id, "success": True}


@app.post("/api/tasks-memory/{task_id}/complete")
async def api_task_memory_complete(task_id: int):
    complete_task(task_id)
    return {"success": True, "id": task_id}


# ---------------------------------------------------------------------------
# Agent / Workflow Status API
# ---------------------------------------------------------------------------

# In-memory agent state (shared across the process)
_agent_state = {
    "status": "idle",  # idle, planning, executing, verifying
    "current_goal": None,
    "last_action": None,
    "action_count": 0,
    "started_at": time.time(),
}
_agent_history: list[dict] = []  # Recent agent actions


@app.get("/api/agent/status")
async def api_agent_status():
    actions = action_log.list_actions(limit=10)
    return {
        **_agent_state,
        "uptime": int(time.time() - _agent_state["started_at"]),
        "recent_actions": actions,
    }


@app.get("/api/agent/history")
async def api_agent_history(limit: int = 30):
    return {"history": action_log.list_actions(limit=limit)}


class GoalSubmit(BaseModel):
    goal: str
    priority: str = "medium"


@app.post("/api/agent/goal")
async def api_agent_goal(body: GoalSubmit):
    """Submit a high-level goal. Creates a task and logs it."""
    task_id = create_task(
        title=body.goal,
        description=f"Goal submitted via UI: {body.goal}",
        priority=body.priority,
    )
    _agent_state["current_goal"] = body.goal
    _agent_state["status"] = "planning"
    action_log.record_action(
        "goal_submitted", f"Goal: {body.goal}",
        risk="low", target="agent",
        details={"goal": body.goal, "priority": body.priority, "task_id": task_id},
    )
    return {"success": True, "task_id": task_id, "goal": body.goal}


@app.get("/api/conversation/summary")
async def api_conversation_summary():
    """Get token usage and conversation stats."""
    uptime = int(time.time() - _session_start)
    today = _get_usage_for_period(86400)
    mapped_session = {
        "input_tokens": _session_tokens.get("input", 0),
        "output_tokens": _session_tokens.get("output", 0),
        "api_calls": _session_tokens.get("api_calls", 0),
        "tts_calls": _session_tokens.get("tts_calls", 0),
    }
    return {
        "uptime_seconds": uptime,
        "session_tokens": mapped_session,
        "today_tokens": today,
        "today_cost": round(_cost_from_tokens(today["input_tokens"], today["output_tokens"]), 4),
        "agent_status": _agent_state["status"],
    }


# ---------------------------------------------------------------------------
# Control endpoints (restart, fix-self)
# ---------------------------------------------------------------------------

@app.post("/api/restart")
async def api_restart():
    """Restart the JARVIS server."""
    log.info("Restart requested — shutting down in 2 seconds")
    async def _restart():
        await asyncio.sleep(2)
        cmd = [sys.executable, __file__, "--port", "8340", "--host", "0.0.0.0"]
        os.execv(sys.executable, cmd)
    asyncio.create_task(_restart())
    return {"status": "restarting"}


@app.post("/api/fix-self")
async def api_fix_self():
    """Enter work mode in the JARVIS repo — JARVIS can now fix himself."""
    jarvis_dir = str(Path(__file__).parent)
    # The work_session is per-WebSocket, so we set a flag that the handler picks up
    # For now, also open Terminal so user can see
    skip_flag = " --dangerously-skip-permissions" if _SKIP_PERMISSIONS else ""
    escaped_jarvis_dir = applescript_escape(jarvis_dir)
    script = (
        'tell application "Terminal"\n'
        '    activate\n'
        f'    do script "cd {escaped_jarvis_dir} && claude{skip_flag}"\n'
        'end tell'
    )
    await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    log.info("Work mode: JARVIS repo opened for self-improvement")
    return {"status": "work_mode_active", "path": jarvis_dir}


# ---------------------------------------------------------------------------
# Static file serving (frontend)
# ---------------------------------------------------------------------------

from starlette.staticfiles import StaticFiles
from starlette.responses import FileResponse

FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"

if FRONTEND_DIST.exists():
    @app.get("/")
    async def serve_index():
        return FileResponse(str(FRONTEND_DIST / "index.html"))

    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="JARVIS Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=8340, help="Bind port")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on changes")
    parser.add_argument("--ssl", action="store_true", help="Enable HTTPS with key.pem/cert.pem")
    args = parser.parse_args()

    # Auto-detect SSL certs
    cert_file = Path(__file__).parent / "cert.pem"
    key_file = Path(__file__).parent / "key.pem"
    use_ssl = args.ssl or (cert_file.exists() and key_file.exists())

    proto = "https" if use_ssl else "http"
    ws_proto = "wss" if use_ssl else "ws"

    print()
    print("  J.A.R.V.I.S. Server v0.1.0")
    print(f"  WebSocket: {ws_proto}://{args.host}:{args.port}/ws/voice")
    print(f"  REST API:  {proto}://{args.host}:{args.port}/api/")
    print(f"  Tasks:     {proto}://{args.host}:{args.port}/api/tasks")
    print()

    ssl_kwargs = {}
    if use_ssl:
        ssl_kwargs["ssl_keyfile"] = str(key_file)
        ssl_kwargs["ssl_certfile"] = str(cert_file)

    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        **ssl_kwargs,
    )
