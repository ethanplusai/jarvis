"""The settings endpoints: the .env the voice page's Settings dialog reads
and writes, the Fish Audio key test, and the two helpers (`_fish_key`,
`_fish_voice`) the TTS path in server.py imports from here. Carved out of
server.py; the value rules (`_env_value_problem`, the header-line regime)
came with it unchanged."""
import logging
import os
import re
import shutil
import time
from pathlib import Path

import json

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import data_paths
import tts
from envfile import parse_env_lines as _parse_env_lines
from usage_api import _session_start

log = logging.getLogger("jarvis")
router = APIRouter()


FISH_VOICE_ID = os.getenv("FISH_VOICE_ID", "612b878b113047d9a770c069c8b4fdfe")  # JARVIS (MCU)


def _fish_key() -> str:
    """The key as it is NOW, not as it was at import.

    `/api/settings/keys` writes .env and os.environ, but every synthesis
    read the import-time constant above, so a key entered in the settings
    panel did nothing until a restart: the panel said "saved" and JARVIS
    went on getting 401 with the placeholder. Measured on a first install.
    """
    return os.getenv("FISH_API_KEY", "")


def _fish_voice() -> str:
    return os.getenv("FISH_VOICE_ID", "") or FISH_VOICE_ID


def _fish_model() -> str:
    """Which Fish Audio model speaks. The service's default is paid; a key
    on an account with no credit gets 402 for every sentence unless
    FISH_MODEL names the free tier (`s2.1-pro-free`)."""
    return os.getenv("FISH_MODEL", "").strip() or tts.DEFAULT_MODEL

# ---------------------------------------------------------------------------
# Settings / Configuration endpoints
# ---------------------------------------------------------------------------

# The only keys any HTTP route may write into .env.
#
# The gate is here, at the one function that writes, rather than on each
# endpoint: `JARVIS_CLAUDE_PATH` is the binary the brain spawns and
# `JARVIS_PROJECT_ROOTS` is what counts as "inside a project" for every
# containment check, so an endpoint that can write an arbitrary key is an
# endpoint that can replace JARVIS's brain with /tmp/evil and then ask for
# a restart.
SETTABLE_ENV_KEYS = frozenset({
    "FISH_API_KEY", "FISH_VOICE_ID", "USER_NAME", "HONORIFIC",
    # The WhatsApp line (whatsapp.py). The key and the id are opaque tokens;
    # the owner's number is checked for its E.164 shape by `whatsapp.issue`
    # before anything is ever sent to it.
    "KAPSO_API_KEY", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_OWNER_NUMBER",
    # The Telegram line (telegram.py). The token is an opaque credential
    # from @BotFather; the owner's id is written by the pairing flow, and
    # both are checked for shape by `telegram.issue` before use.
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID",
})

# A value may not carry anything that ends the line it is written on.
#
# Asked of the READER (`_parse_env_lines`, at the top of this file), never of
# a hand-written list of characters. The list was "\n", "\r", "\0"; the
# readers split with `str.splitlines()`, which splits on ten characters, so
# `\x0b`, `\x0c`, `\x1c`, `\x1d`, `\x1e`, `\x85`, ` ` and ` ` each
# wrote a whole extra setting into `.env` through a 200 OK.
#
# The rule now is a round trip: JARVIS will write `key=value` only if reading
# that back gives exactly this key and exactly this value. It refuses more
# than line breaks — a leading space or a wrapping quote would be silently
# eaten by the reader too, and saying "saved" while storing something else is
# the same class of lie as reporting a stalled run as a success.
# And a value has a LENGTH.
#
# There was no bound at all, and `USER_NAME` is not an ordinary setting: it
# is spliced into every generation's system prompt by `brain.launch_prompt`
# ("The user's name is {…}") with nothing around it. A 100 KB `USER_NAME`
# posted to `/api/settings/preferences` round-tripped through this function,
# through `.env`, and into the brain — a paragraph of somebody's choosing
# standing in the system prompt as JARVIS's own words.
#
# So two bounds, because the keys are two kinds of thing. A Fish Audio key is
# an opaque token and needs room; a NAME is a name. Neither needs a thousand
# characters, and the name — the one that reaches the prompt — gets the
# tighter one. Held against SETTABLE_ENV_KEYS itself by tests/test_bounds.py,
# so a key added later is bounded the day it is added.
ENV_VALUE_MAX_CHARS = 500
ENV_NAME_KEYS = frozenset({"USER_NAME", "HONORIFIC"})
ENV_NAME_MAX_CHARS = 64

# `str.splitlines()` covers the ten separators; this covers what is left of
# C0/C1 and DEL. An ESC is neither a separator nor printable, and it went
# into the system prompt — and into whatever renders it — untouched.
_ENV_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _env_value_max(key: str) -> int:
    return ENV_NAME_MAX_CHARS if key in ENV_NAME_KEYS else ENV_VALUE_MAX_CHARS


def _env_value_problem(key: str, value: str) -> str | None:
    """Why `key=value` cannot be written into `.env`, or None if it can."""
    if len(value) > _env_value_max(key):
        return (f"That setting is too long — {_env_value_max(key)} "
                f"characters at most")
    if "\0" in value:
        # `splitlines()` does not split on NUL, so the round trip below would
        # not catch it — but it truncates the string for anything that hands
        # the value to a C API, so it keeps its own rule.
        return "A setting cannot contain a null byte"
    if value.splitlines() != ([value] if value else []):
        return "A setting cannot contain a line break"
    if _ENV_CONTROL_RE.search(value):
        return "A setting cannot contain a control character"
    if _parse_env_lines(f"{key}={value}") != [(key, value)]:
        return "A setting cannot begin or end with a space or a quote"
    return None


def _env_file_path() -> Path:
    # JARVIS_ENV_FILE exists so the test suite cannot write into the
    # developer's live .env — the same reasoning as JARVIS_DATA_DIR.
    override = os.getenv("JARVIS_ENV_FILE", "").strip()
    return Path(override) if override else Path(__file__).parent / ".env"

def _env_example_path() -> Path:
    return Path(__file__).parent / ".env.example"

def _read_env(create: bool = False) -> tuple[list[str], dict[str, str]]:
    """Read .env. Returns (raw_lines, parsed_dict).

    `create` seeds the file from .env.example, and only a caller that is
    about to write may ask for it. It used to happen unconditionally, which
    made `GET /api/settings/status` — a read, by every reading of its name —
    create a file on disk as a side effect of being asked a question.
    """
    path = _env_file_path()
    if not path.exists():
        if not create:
            return [], {}
        path.parent.mkdir(parents=True, exist_ok=True)
        example = _env_example_path()
        if example.exists():
            import shutil as _shutil
            _shutil.copy2(str(example), str(path))
        else:
            path.write_text("", encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    # The same parser the boot loader uses, and the same one the writer asks
    # before it commits a value — see `_parse_env_lines`.
    parsed: dict[str, str] = dict(_parse_env_lines(text))
    return lines, parsed

def _write_env_key(key: str, value: str) -> None:
    """Update a single key in .env, preserving comments and order.

    Raises ValueError for a key nobody may set, or a value the reader would
    not read back as written: `f"{key}={value}"` with anything
    `str.splitlines()` splits on in `value` appends whatever follows it as a
    separate setting.
    """
    if key not in SETTABLE_ENV_KEYS:
        raise ValueError(f"{key} is not a setting JARVIS will write")
    problem = _env_value_problem(key, value)
    if problem:
        raise ValueError(problem)
    lines, _ = _read_env(create=True)
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
    _env_file_path().write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    os.environ[key] = value

class KeyUpdate(BaseModel):
    key_name: str
    key_value: str

class KeyTest(BaseModel):
    key_value: str | None = None

class PreferencesUpdate(BaseModel):
    user_name: str = ""
    honorific: str = "sir"

@router.post("/api/settings/keys")
async def api_settings_keys(body: KeyUpdate):
    try:
        _write_env_key(body.key_name, body.key_value)
    except ValueError as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    return {"success": True}

@router.post("/api/settings/test-fish")
async def api_test_fish(body: KeyTest):
    key = body.key_value or os.getenv("FISH_API_KEY", "")
    if not key:
        return {"valid": False, "error": "No key provided"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.fish.audio/v1/tts",
                # The same model the voice path uses, or the test passes on a
                # model JARVIS will never speak with.
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                         "model": _fish_model()},
                json={"text": "test", "reference_id": _fish_voice()},
            )
            if resp.status_code in (200, 201):
                return {"valid": True}
            elif resp.status_code == 401:
                return {"valid": False, "error": "Invalid API key"}
            elif resp.status_code == 402:
                # The key is real; the account behind it cannot pay for the
                # sentence. "HTTP 402" sent a user back to re-paste a key that
                # was never the problem.
                return {"valid": False,
                        "error": "Key accepted, but the Fish Audio account has no "
                                 "credit for this model. Top it up at fish.audio, or "
                                 "set FISH_MODEL=s2.1-pro-free in .env for the free "
                                 "tier, then test again."}
            else:
                return {"valid": False, "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}

@router.get("/api/settings/status")
async def api_settings_status():
    import shutil as _shutil
    _, env_dict = _read_env()
    claude_installed = _shutil.which("claude") is not None
    return {
        "claude_code_installed": claude_installed,
        "server_port": int(os.getenv("JARVIS_PORT", "8340")),
        "uptime_seconds": int(time.time() - _session_start),
        "env_keys_set": {
            "fish_audio": bool(env_dict.get("FISH_API_KEY", "").strip() and env_dict.get("FISH_API_KEY", "") != "your-fish-audio-api-key-here"),
            "fish_voice_id": bool(env_dict.get("FISH_VOICE_ID", "").strip()),
            "user_name": env_dict.get("USER_NAME", ""),
            "kapso_api_key": bool(env_dict.get("KAPSO_API_KEY", "").strip()),
            "whatsapp_phone_number_id": bool(env_dict.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()),
            "whatsapp_owner_number": bool((env_dict.get("WHATSAPP_OWNER_NUMBER", "")
                                           or env_dict.get("JARVIS_OWNER_PHONE", "")).strip()),
            "telegram_bot_token": bool(env_dict.get("TELEGRAM_BOT_TOKEN", "").strip()),
            "telegram_owner_id": bool(env_dict.get("TELEGRAM_OWNER_ID", "").strip()),
        },
    }

@router.get("/api/settings/preferences")
async def api_get_preferences():
    _, env_dict = _read_env()
    return {
        "user_name": env_dict.get("USER_NAME", ""),
        "honorific": env_dict.get("HONORIFIC", "sir"),
    }

@router.post("/api/settings/preferences")
async def api_save_preferences(body: PreferencesUpdate):
    # Validate both before writing either, so a bad honorific cannot leave
    # the name half-saved.
    try:
        for key, value in (("USER_NAME", body.user_name),
                           ("HONORIFIC", body.honorific)):
            problem = _env_value_problem(key, value)
            if problem:
                raise ValueError(problem)
        _write_env_key("USER_NAME", body.user_name)
        _write_env_key("HONORIFIC", body.honorific)
    except ValueError as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    return {"success": True}
