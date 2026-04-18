"""
Settings API — /api/settings/* endpoints for key management, connectivity
checks, and user preferences.

Exports an APIRouter. server.py attaches it to the main FastAPI app and
passes the require_auth dependency + FISH_VOICE_ID config in via
install_settings_router().
"""

import contextlib
import logging
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path

import anthropic
import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from calendar_access import get_todays_events
from mail_access import get_unread_count
from memory import get_important_memories, get_open_tasks
from notes_access import get_recent_notes
from usage import SESSION_START

log = logging.getLogger("jarvis.api_settings")


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
            shutil.copy2(str(example), str(path))
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


class PreferencesUpdate(BaseModel):
    user_name: str = ""
    honorific: str = "sir"
    calendar_accounts: str = "auto"


def build_settings_router(require_auth: Callable, fish_voice_id: str) -> APIRouter:
    """Build the /api/settings/* router with injected auth + config."""
    router = APIRouter(prefix="/api/settings", dependencies=[Depends(require_auth)])

    @router.post("/keys")
    async def api_settings_keys(body: KeyUpdate):
        allowed = {"ANTHROPIC_API_KEY", "FISH_API_KEY", "FISH_VOICE_ID", "USER_NAME", "HONORIFIC", "CALENDAR_ACCOUNTS"}
        if body.key_name not in allowed:
            return JSONResponse({"success": False, "error": "Invalid key name"}, status_code=400)
        _write_env_key(body.key_name, body.key_value)
        return {"success": True}

    @router.post("/test-anthropic")
    async def api_test_anthropic(body: KeyTest):
        key = body.key_value or os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            return {"valid": False, "error": "No key provided"}
        try:
            client = anthropic.AsyncAnthropic(api_key=key)
            await client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=10, messages=[{"role": "user", "content": "Hi"}]
            )
            return {"valid": True}
        except Exception as e:
            return {"valid": False, "error": str(e)[:200]}

    @router.post("/test-fish")
    async def api_test_fish(body: KeyTest):
        key = body.key_value or os.getenv("FISH_API_KEY", "")
        if not key:
            return {"valid": False, "error": "No key provided"}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    "https://api.fish.audio/v1/tts",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"text": "test", "reference_id": fish_voice_id},
                )
                if resp.status_code in (200, 201):
                    return {"valid": True}
                elif resp.status_code == 401:
                    return {"valid": False, "error": "Invalid API key"}
                else:
                    return {"valid": False, "error": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"valid": False, "error": str(e)[:200]}

    @router.get("/status")
    async def api_settings_status():
        _, env_dict = _read_env()
        claude_installed = shutil.which("claude") is not None
        calendar_ok = mail_ok = notes_ok = False
        with contextlib.suppress(Exception):
            await get_todays_events()
            calendar_ok = True
        with contextlib.suppress(Exception):
            await get_unread_count()
            mail_ok = True
        with contextlib.suppress(Exception):
            await get_recent_notes(count=1)
            notes_ok = True
        memory_count = task_count = 0
        with contextlib.suppress(Exception):
            memory_count = len(get_important_memories(limit=9999))
        with contextlib.suppress(Exception):
            task_count = len(get_open_tasks())
        return {
            "claude_code_installed": claude_installed,
            "calendar_accessible": calendar_ok,
            "mail_accessible": mail_ok,
            "notes_accessible": notes_ok,
            "memory_count": memory_count,
            "task_count": task_count,
            "server_port": 8340,
            "uptime_seconds": int(time.time() - SESSION_START),
            "env_keys_set": {
                "anthropic": bool(
                    env_dict.get("ANTHROPIC_API_KEY", "").strip()
                    and env_dict.get("ANTHROPIC_API_KEY", "") != "your-anthropic-api-key-here"
                ),
                "fish_audio": bool(
                    env_dict.get("FISH_API_KEY", "").strip()
                    and env_dict.get("FISH_API_KEY", "") != "your-fish-audio-api-key-here"
                ),
                "fish_voice_id": bool(env_dict.get("FISH_VOICE_ID", "").strip()),
                "user_name": env_dict.get("USER_NAME", ""),
            },
        }

    @router.get("/preferences")
    async def api_get_preferences():
        _, env_dict = _read_env()
        return {
            "user_name": env_dict.get("USER_NAME", ""),
            "honorific": env_dict.get("HONORIFIC", "sir"),
            "calendar_accounts": env_dict.get("CALENDAR_ACCOUNTS", "auto"),
        }

    @router.post("/preferences")
    async def api_save_preferences(body: PreferencesUpdate):
        _write_env_key("USER_NAME", body.user_name)
        _write_env_key("HONORIFIC", body.honorific)
        _write_env_key("CALENDAR_ACCOUNTS", body.calendar_accounts)
        return {"success": True}

    return router
