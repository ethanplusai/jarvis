"""JARVIS on WhatsApp — the Kapso transport for the owner's phone.

The first door is the browser tab: a microphone, a speaker, and the Business
desk where approvals are decided. It is useless the moment the user walks
away from the machine, which is exactly when a Claude Code session stops to
ask a question, a build fails, or a connector call sits held for two minutes
waiting for a yes. This module is one of the two lines (`messaging.py`
holds what they share; `telegram.py` is the other) that give him a way to
reach the user, and the user a way to answer.

**Kapso.** The number and the API come from Kapso (kapso.ai), a Meta
Business Partner that proxies the official WhatsApp Cloud API unchanged
(`/meta/whatsapp/v24.0/...`, the Graph API's own payloads) behind one
`X-API-Key` header, and — the part that matters here — STORES every message
so it can be read back with `GET /{phone_number_id}/messages`. Everything is
plain `httpx` against that proxy; no SDK, because this repository adds no
dependencies and the calls are four URLs.

**Polling, not a webhook.** JARVIS binds to loopback on purpose (see
`web_auth.py`) and the whole approval boundary rests on nothing from the
network being able to reach it. A webhook needs a public HTTPS address — a
tunnel — which is that exposure by another name. So the inbound half reads
Kapso's stored messages on a timer instead: every few seconds while a reply
is expected, every twenty otherwise (`_interval`). Each message id is claimed
in SQLite before it is acted on (`_claim`), so a poll that overlaps the last
one, a restart, or Kapso returning the same page twice can never run a
message a second time.

**Owner only, both ways.** There is no `to` parameter anywhere in this
module: every send goes to the configured owner. And every inbound message is
matched against that same number before anything reads it; a stranger's
message is claimed (never re-read), counted, and dropped — the brain never
sees it. What an owner's message DOES is not decided here: it is handed to
`messaging.on_text` / `on_button` / `on_other`, the same policy the Telegram
line uses, and every decision it makes is `business_api.decide`, recorded
as `via:whatsapp`.

**No calls.** Meta's Business Calling API, which Kapso proxies as-is, needs
the caller to bring a WebRTC media stack: an SDP offer, ICE, DTLS-SRTP and
Opus in real time. A Python process with `httpx` cannot do that, and Meta
does not offer business-initiated calls from US numbers (the only kind Kapso
provisions for free) in any case. What "JARVIS calls you" means here is a
**voice note in his own voice** — Fish Audio, opus — sent with the urgent
lines; a ringing phone stays on the Twilio provider in
`business_providers.py`. Said in docs/whatsapp.md so nobody rediscovers it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, NamedTuple, Optional

import httpx

import messaging
from data_paths import db_path
from messaging import ChannelError, NotConfigured, chunks, clean as _clean, sniff_audio

log = logging.getLogger("jarvis.whatsapp")

NAME = "whatsapp"
LABEL = "WhatsApp"
DEFAULT_BASE_URL = "https://api.kapso.ai"
GRAPH_VERSION = "v24.0"

# Meta's own limits, checked here so a refused send is refused with a reason
# rather than a 400 from the far end.
TEXT_BODY_MAX = 4096
TEXT_CHUNK = messaging.TEXT_CHUNK
INTERACTIVE_BODY_MAX = 1024
BUTTON_TITLE_MAX = 20
BUTTON_ID_MAX = 256
TEMPLATE_PARAM_MAX = 900    # the whole rendered body must stay under 1024
# What the policy asks of this line: how long a card body may be, and how
# much of the digest a button id can carry (all of it, here).
BODY_MAX = INTERACTIVE_BODY_MAX
BUTTON_DIGEST_CHARS = 64

# Meta error 131047: the 24-hour customer-service window is shut, and only an
# approved template can open it. Everything else is a plain failure.
WINDOW_CLOSED_CODE = 131047

# How far back the first poll after a start looks. A message the owner sent
# while JARVIS was down is still worth answering a few minutes later; one
# from last night is not, and an "approve" from last night is exactly what
# must not be acted on out of the blue.
CATCHUP_SEC = 15 * 60
# Overlap between polls, so a message written to Kapso a moment before the
# cursor moved is still seen. Duplicates are harmless: `_claim` drops them.
OVERLAP_SEC = 120
# How long after JARVIS sends, or the owner writes, the poll stays quick.
HOT_FOR_SEC = 10 * 60
DEFAULT_HOT_SEC = 3.0
DEFAULT_IDLE_SEC = 20.0
POLL_PAGE_LIMIT = 100
POLL_MAX_PAGES = 5
REQUEST_TIMEOUT = 15.0
MEDIA_TIMEOUT = 40.0

_E164 = re.compile(r"\+[1-9][0-9]{7,14}")
_PHONE_NUMBER_ID = re.compile(r"[0-9]{5,25}")
_TEMPLATE_NAME = re.compile(r"[a-z0-9_]{1,512}")
_LANGUAGE = re.compile(r"[a-z]{2}(?:_[A-Z]{2})?")
_TEMPLATE_WS = re.compile(r"[\s]+")


class WindowClosed(ChannelError):
    """Meta's 24-hour window is shut and no template is configured to open it."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _flag(name: str, default: bool) -> bool:
    value = env(name).lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def _seconds(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(env(name) or default)
    except ValueError:
        return default
    return min(max(value, low), high)


def owner_number() -> str:
    """The owner's number in E.164: WhatsApp's own, or the one Twilio calls."""
    return env("WHATSAPP_OWNER_NUMBER") or env("JARVIS_OWNER_PHONE")


def _digits(value: str) -> str:
    return re.sub(r"[^0-9]", "", value or "")


class Config(NamedTuple):
    api_key: str
    phone_number_id: str
    owner: str                  # E.164, as configured
    owner_wa_id: str            # what `from` looks like on an inbound message
    base_url: str
    template: str               # an approved utility template, or ""
    template_language: str
    inbound: bool               # read the owner's messages at all
    approvals: bool             # let a button or an "approve" decide a card
    voice_notes: bool           # urgent lines also as a voice note
    hot_sec: float
    idle_sec: float


def missing() -> list[str]:
    """The names still unset among what the channel cannot work without."""
    out = []
    if not env("KAPSO_API_KEY"):
        out.append("KAPSO_API_KEY")
    if not env("WHATSAPP_PHONE_NUMBER_ID"):
        out.append("WHATSAPP_PHONE_NUMBER_ID")
    if not owner_number():
        out.append("WHATSAPP_OWNER_NUMBER")
    return out


def issue() -> Optional[str]:
    """Why a fully-filled configuration still cannot be used, or None."""
    if missing():
        return None
    if not _E164.fullmatch(owner_number()):
        return "The owner's number must be in E.164 form, like +14155550132"
    if not _PHONE_NUMBER_ID.fullmatch(env("WHATSAPP_PHONE_NUMBER_ID")):
        return "WHATSAPP_PHONE_NUMBER_ID is Meta's numeric id for the number, not the number itself"
    if any(ch.isspace() for ch in env("KAPSO_API_KEY")):
        return "KAPSO_API_KEY contains whitespace"
    template = env("WHATSAPP_TEMPLATE")
    if template and not _TEMPLATE_NAME.fullmatch(template):
        return "WHATSAPP_TEMPLATE must be the template's name: lowercase letters, digits and underscores"
    language = env("WHATSAPP_TEMPLATE_LANGUAGE")
    if language and not _LANGUAGE.fullmatch(language):
        return "WHATSAPP_TEMPLATE_LANGUAGE must look like en or en_US"
    return None


def touched() -> bool:
    """Whether the user has started setting the channel up at all."""
    return bool(env("KAPSO_API_KEY") or env("WHATSAPP_PHONE_NUMBER_ID")
                or env("WHATSAPP_OWNER_NUMBER"))


def config() -> Optional[Config]:
    """The channel's configuration, or None when it cannot be used."""
    if missing() or issue():
        return None
    owner = owner_number()
    return Config(
        api_key=env("KAPSO_API_KEY"),
        phone_number_id=env("WHATSAPP_PHONE_NUMBER_ID"),
        owner=owner,
        owner_wa_id=_digits(env("WHATSAPP_OWNER_WA_ID")) or _digits(owner),
        base_url=(env("KAPSO_API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
        template=env("WHATSAPP_TEMPLATE"),
        template_language=env("WHATSAPP_TEMPLATE_LANGUAGE") or "en_US",
        inbound=_flag("WHATSAPP_INBOUND", True),
        approvals=_flag("WHATSAPP_APPROVALS", True),
        voice_notes=_flag("WHATSAPP_VOICE_NOTES", True),
        hot_sec=_seconds("WHATSAPP_POLL_HOT_SECONDS", DEFAULT_HOT_SEC, 1.0, 60.0),
        idle_sec=_seconds("WHATSAPP_POLL_IDLE_SECONDS", DEFAULT_IDLE_SEC, 2.0, 600.0),
    )


def configured() -> bool:
    return config() is not None


def owner_key() -> Optional[str]:
    """Whose line this is right now, or None — see `telegram.owner_key`."""
    cfg = config()
    return cfg.owner_wa_id if cfg else None


def approvals() -> bool:
    return _flag("WHATSAPP_APPROVALS", True)


def voice_notes() -> bool:
    return _flag("WHATSAPP_VOICE_NOTES", True)


def require() -> Config:
    cfg = config()
    if cfg is None:
        names = missing()
        raise NotConfigured(
            "WhatsApp is not set up: set " + ", ".join(names) + " in .env"
            if names else "WhatsApp is not usable: " + str(issue()))
    return cfg


def masked(number: str) -> str:
    """A number as the status report shows it: the last four digits only."""
    digits = _digits(number)
    return ("•••" + digits[-4:]) if len(digits) >= 4 else ("•••" if digits else "")


# ---------------------------------------------------------------------------
# Tables: the claim on each inbound id, the cursor, and a bounded log
# ---------------------------------------------------------------------------

LOG_ROWS_MAX = 2000


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path()), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(_connect()) as conn, conn:
        conn.executescript("""
          CREATE TABLE IF NOT EXISTS whatsapp_messages (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            wamid TEXT UNIQUE NOT NULL,
            direction TEXT NOT NULL,
            kind TEXT NOT NULL,
            at REAL NOT NULL,
            summary TEXT NOT NULL DEFAULT '');
          CREATE TABLE IF NOT EXISTS whatsapp_state (
            key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)


def _state_get(key: str) -> str:
    try:
        with closing(_connect()) as conn:
            row = conn.execute("SELECT value FROM whatsapp_state WHERE key=?", (key,)).fetchone()
            return str(row[0]) if row else ""
    except sqlite3.Error:
        return ""


def _state_set(key: str, value: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("INSERT INTO whatsapp_state(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _claim(wamid: str, direction: str, kind: str, at: float, summary: str = "") -> bool:
    """Record one message id once. True the first time, False ever after —
    the whole guarantee that nothing here runs twice."""
    if not wamid:
        return False
    with closing(_connect()) as conn, conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO whatsapp_messages(wamid,direction,kind,at,summary) "
            "VALUES(?,?,?,?,?)", (wamid, direction, kind, at, summary[:200]))
        first = cursor.rowcount == 1
        if first:
            conn.execute("DELETE FROM whatsapp_messages WHERE seq <= "
                         "(SELECT MAX(seq) FROM whatsapp_messages) - ?", (LOG_ROWS_MAX,))
        return first


def _last(direction: str) -> Optional[float]:
    try:
        with closing(_connect()) as conn:
            row = conn.execute("SELECT MAX(at) FROM whatsapp_messages WHERE direction=?",
                               (direction,)).fetchone()
            return float(row[0]) if row and row[0] is not None else None
    except sqlite3.Error:
        return None


def recent(limit: int = 20) -> list[dict]:
    """The last few messages either way, for the status page: id, direction,
    kind, time and a short summary — never a secret, see `_send`."""
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT wamid,direction,kind,at,summary FROM whatsapp_messages "
                            "ORDER BY seq DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
        return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# The Kapso client
# ---------------------------------------------------------------------------

_client: Optional[httpx.AsyncClient] = None
_client_key: tuple = ()


def _new_client(base_url: str, api_key: str) -> httpx.AsyncClient:
    """The one place a client is made — tests replace it with a MockTransport."""
    return httpx.AsyncClient(base_url=base_url, timeout=REQUEST_TIMEOUT,
                             follow_redirects=False,
                             headers={"X-API-Key": api_key, "Accept": "application/json"})


async def _get_client(cfg: Config) -> httpx.AsyncClient:
    global _client, _client_key
    key = (cfg.base_url, cfg.api_key)
    if _client is None or _client_key != key:
        if _client is not None:
            try:
                await _client.aclose()
            except Exception:
                pass
        _client = _new_client(cfg.base_url, cfg.api_key)
        _client_key = key
    return _client


async def close() -> None:
    global _client, _client_key
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass
    _client, _client_key = None, ()


def _error_of(body: Any, status: int) -> ChannelError:
    """A ChannelError out of whatever the far end sent: the status and the
    code in the sentence, the provider's own words only in `detail`."""
    code = None
    detail = ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            try:
                code = int(err.get("code")) if err.get("code") is not None else None
            except (TypeError, ValueError):
                code = None
            detail = str(err.get("message") or err.get("error_user_msg") or "")
        elif isinstance(body.get("message"), str):
            detail = body["message"]
    detail = _clean(detail)[:200]
    message = f"WhatsApp returned HTTP {status}" + (f" (code {code})" if code is not None else "")
    if code == WINDOW_CLOSED_CODE:
        return WindowClosed(message, status=status, code=code, detail=detail)
    return ChannelError(message, status=status, code=code, detail=detail)


async def _request(method: str, path: str, *, params: Optional[dict] = None,
                   json_body: Optional[dict] = None, data: Optional[dict] = None,
                   files: Optional[dict] = None, timeout: float = REQUEST_TIMEOUT) -> dict:
    """One call to the proxy. Raises ChannelError (WindowClosed for 131047)."""
    cfg = require()
    client = await _get_client(cfg)
    url = f"/meta/whatsapp/{GRAPH_VERSION}{path}"
    try:
        response = await client.request(method, url, params=params, json=json_body,
                                        data=data, files=files, timeout=timeout)
    except httpx.HTTPError as e:
        raise ChannelError(f"Could not reach WhatsApp: {type(e).__name__}") from None
    try:
        body = response.json() if response.content else {}
    except ValueError:
        body = {}
    if not 200 <= response.status_code < 300:
        raise _error_of(body, response.status_code)
    if not isinstance(body, dict):
        raise ChannelError("WhatsApp answered with something that is not an object")
    return body


# ---------------------------------------------------------------------------
# Sending — always to the owner
# ---------------------------------------------------------------------------

_hot_until = 0.0
_last_error: Optional[str] = None


def _went_hot() -> None:
    global _hot_until
    _hot_until = time.monotonic() + HOT_FOR_SEC


async def _send(payload: dict, *, kind: str, summary: str = "",
                timeout: float = REQUEST_TIMEOUT) -> dict:
    """POST one message to the owner. `to` is set HERE and nowhere else."""
    global _last_error
    cfg = require()
    body = {"messaging_product": "whatsapp", "recipient_type": "individual",
            "to": cfg.owner_wa_id, **payload}
    try:
        reply = await _request("POST", f"/{cfg.phone_number_id}/messages", json_body=body,
                               timeout=timeout)
    except ChannelError as e:
        _last_error = e.report
        raise
    _last_error = None
    messages = reply.get("messages") if isinstance(reply, dict) else None
    wamid = ""
    if isinstance(messages, list) and messages and isinstance(messages[0], dict):
        wamid = str(messages[0].get("id") or "")
    _claim(wamid or f"out:{time.time_ns()}", "outbound", kind, time.time(), summary)
    _went_hot()
    return {"wamid": wamid, "kind": kind}


async def send_text(text: str) -> dict:
    """One text message, as given (the caller has already chunked it)."""
    text = _clean(text)
    if not 1 <= len(text) <= TEXT_BODY_MAX:
        raise ChannelError(f"A text message is 1 to {TEXT_BODY_MAX} characters")
    return await _send({"type": "text", "text": {"body": text, "preview_url": False}},
                       kind="text", summary=text[:120])


def _template_param(text: str) -> str:
    """What Meta lets a body parameter be: one line, no runs of spaces."""
    return _TEMPLATE_WS.sub(" ", _clean(text)).strip()[:TEMPLATE_PARAM_MAX]


async def send_template(text: str) -> dict:
    """The configured utility template with `text` as its one `message`
    parameter — the only thing that can be sent once the 24-hour window
    has shut. `scripts/whatsapp_setup.py template` creates one."""
    cfg = require()
    if not cfg.template:
        raise WindowClosed("The 24-hour window is shut and no WHATSAPP_TEMPLATE is set")
    payload = {"type": "template", "template": {
        "name": cfg.template, "language": {"code": cfg.template_language},
        "components": [{"type": "body", "parameters": [
            {"type": "text", "parameter_name": "message", "text": _template_param(text)}]}]}}
    return await _send(payload, kind="template", summary=text[:120])


async def deliver(text: str) -> dict:
    """Say `text` to the owner, however long, however the window stands.

    Split to fit, sent as text; when the window is shut (WindowClosed) and a
    template is configured, that template carries the same words instead.
    Returns the last receipt with `via` set to "text" or "template".
    """
    pieces = chunks(text)
    if not pieces:
        raise ChannelError("Nothing to send")
    last: dict = {}
    total = len(pieces)
    for index, piece in enumerate(pieces, 1):
        marked = f"({index}/{total}) {piece}" if total > 1 else piece
        try:
            last = {**(await send_text(marked)), "via": "text"}
        except WindowClosed:
            if not require().template:
                raise
            last = {**(await send_template(marked)), "via": "template"}
    return last


async def send_buttons(body: str, buttons: list[tuple[str, str]]) -> dict:
    """An interactive message with up to three reply buttons: (id, title)."""
    body = _clean(body)
    if not 1 <= len(body) <= INTERACTIVE_BODY_MAX:
        raise ChannelError(f"A button message's body is 1 to {INTERACTIVE_BODY_MAX} characters")
    if not 1 <= len(buttons) <= 3:
        raise ChannelError("One to three buttons")
    action = []
    for button_id, title in buttons:
        button_id, title = _clean(button_id), _clean(title)
        if not 1 <= len(button_id) <= BUTTON_ID_MAX or not 1 <= len(title) <= BUTTON_TITLE_MAX:
            raise ChannelError("A button needs an id under 256 and a title under 20 characters")
        action.append({"type": "reply", "reply": {"id": button_id, "title": title}})
    payload = {"type": "interactive", "interactive": {
        "type": "button", "body": {"text": body}, "action": {"buttons": action}}}
    return await _send(payload, kind="buttons", summary=body[:120])


async def announce_card(action: dict, body: str, buttons: list[tuple[str, str]]) -> bool:
    """The policy's card, on this wire: buttons, or — when the 24-hour
    window is shut and only a template can get through — one line saying
    what is waiting and how to answer it in words. Never raises."""
    try:
        await send_buttons(body, buttons)
        return True
    except WindowClosed:
        card_id = str(action.get("id", ""))[:8]
        try:
            await deliver(f"Approval needed, sir: {messaging.name(action.get('provider'))} · "
                          f"{messaging.name(action.get('operation'))}, card {card_id}. "
                          f"Reply 'approve {card_id}' or 'reject {card_id}', or open the "
                          f"Business desk.")
            return True
        except ChannelError as e:
            log.warning("whatsapp: card not announced: %s", e.report)
            return False
    except ChannelError as e:
        log.warning("whatsapp: card not announced: %s", e.report)
        return False


async def upload_media(audio: bytes, mime: str) -> str:
    """Upload one media blob; returns Meta's media id."""
    cfg = require()
    ext = {"audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/wav": "wav"}.get(mime, "bin")
    reply = await _request(
        "POST", f"/{cfg.phone_number_id}/media",
        data={"messaging_product": "whatsapp", "type": mime},
        files={"file": (f"jarvis.{ext}", audio, mime)}, timeout=MEDIA_TIMEOUT)
    media_id = str(reply.get("id") or "")
    if not media_id:
        raise ChannelError("WhatsApp accepted the upload but returned no media id")
    return media_id


async def send_voice_note(audio: bytes) -> dict:
    """JARVIS's voice, as a voice note when the bytes allow it."""
    if not audio:
        raise ChannelError("No audio to send")
    mime, voice = sniff_audio(audio)
    media_id = await upload_media(audio, mime)
    body: dict = {"id": media_id}
    if voice:
        body["voice"] = True
    return await _send({"type": "audio", "audio": body},
                       kind="voice" if voice else "audio", summary="(voice note)")


async def mark_read(wamid: str, *, typing: bool = False) -> None:
    """The two ticks, and the typing indicator while the brain thinks.
    Best effort: a failure here is not a failure of anything."""
    if not wamid:
        return
    cfg = config()
    if cfg is None:
        return
    body = {"messaging_product": "whatsapp", "status": "read", "message_id": wamid}
    if typing:
        body["typing_indicator"] = {"type": "text"}
    try:
        await _request("POST", f"/{cfg.phone_number_id}/messages", json_body=body)
    except ChannelError as e:
        log.debug("mark_read failed: %s", e)


# The line-local sends the WhatsApp routes and tests use. The server goes
# through `messaging.reach` / `messaging.say`, which fan out to every line.

async def reach(line: str, *, voice: bool = False) -> bool:
    """Tell the owner `line` on THIS line — text, and a voice note too when
    `voice` and the setting allow it. Never raises; False when nothing went."""
    global _last_error
    cfg = config()
    if cfg is None:
        return False
    try:
        await deliver(line)
    except ChannelError as e:
        _last_error = e.report
        log.warning("whatsapp: could not reach the owner: %s", e.report)
        return False
    except Exception:
        log.warning("whatsapp: could not reach the owner", exc_info=True)
        return False
    if voice and cfg.voice_notes:
        audio = await messaging._synthesise(line)
        if audio:
            await messaging._voice_on(_this(), audio)
    return True


async def say(text: str, *, voice: bool = False) -> dict:
    """Text on this line, plus a voice note on request. Raises ChannelError
    so the caller can say why."""
    receipt = await deliver(text)
    voice_note = False
    if voice:
        audio = await messaging._synthesise(text)
        voice_note = bool(audio) and await messaging._voice_on(_this(), audio)
    receipt["voice_note"] = voice_note
    return receipt


def _this():
    import sys
    return sys.modules[__name__]


def button_ids(action: dict) -> tuple[str, str]:
    """This line's button ids: the card's id and its WHOLE digest."""
    return messaging.button_ids(action, BUTTON_DIGEST_CHARS)


def card_text(action: dict) -> str:
    """The card as this line shows it, under WhatsApp's interactive limit."""
    return messaging.card_text(action, body_max=BODY_MAX)


async def notify_card(action: dict) -> bool:
    """A new pending card → this line, with buttons. Never raises."""
    if config() is None or not isinstance(action, dict) or action.get("state") != "pending":
        return False
    return await messaging.announce(_this(), action)


# ---------------------------------------------------------------------------
# Inbound
# ---------------------------------------------------------------------------

class Inbound(NamedTuple):
    wamid: str
    sender: str         # wa_id digits
    at: float           # epoch seconds
    kind: str           # text | button | other
    text: str
    button_id: str
    forwarded: bool = False     # somebody else's words, sent on by the owner


def _epoch(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def parse_inbound(item: Any) -> Optional[Inbound]:
    """One stored message as the poller sees it, or None if it is not an
    inbound message at all (Kapso's own echo of what JARVIS sent)."""
    if not isinstance(item, dict):
        return None
    kapso = item.get("kapso") if isinstance(item.get("kapso"), dict) else {}
    if str(kapso.get("direction") or "inbound").lower() != "inbound":
        return None
    wamid = str(item.get("id") or "")
    if not wamid:
        return None
    sender = _digits(str(item.get("from") or ""))
    at = _epoch(item.get("timestamp"))
    kind, text, button_id = "other", "", ""
    mtype = str(item.get("type") or "")
    if mtype == "text" and isinstance(item.get("text"), dict):
        kind, text = "text", str(item["text"].get("body") or "")
    elif mtype == "interactive" and isinstance(item.get("interactive"), dict):
        inter = item["interactive"]
        reply = inter.get("button_reply") if inter.get("type") == "button_reply" else \
            inter.get("list_reply") if inter.get("type") == "list_reply" else None
        if isinstance(reply, dict):
            kind, button_id, text = "button", str(reply.get("id") or ""), str(reply.get("title") or "")
    elif mtype == "button" and isinstance(item.get("button"), dict):
        # A quick-reply button on a template message.
        kind = "button"
        button_id = str(item["button"].get("payload") or "")
        text = str(item["button"].get("text") or "")
    # Meta marks a forward in `context`; a reply's `context` names the
    # message it answers instead, and its text is the owner's own.
    context = item.get("context") if isinstance(item.get("context"), dict) else {}
    forwarded = bool(context.get("forwarded") or context.get("frequently_forwarded"))
    return Inbound(wamid, sender, at, kind, _clean(text), _clean(button_id), forwarded)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(max(0.0, epoch), timezone.utc).isoformat(timespec="seconds")


_poller: Optional[asyncio.Task] = None
_last_poll: Optional[float] = None
_ignored_strangers = 0
_errors_in_a_row = 0


async def poll_once() -> int:
    """Read what the owner has sent since the last look, act on each new
    message once, and move the cursor. Returns how many were acted on."""
    global _last_poll, _ignored_strangers
    cfg = require()
    now = time.time()
    floor = now - CATCHUP_SEC
    cursor = _epoch(_state_get("inbound_since"))
    since = max(cursor - OVERLAP_SEC, floor)
    params: dict = {"direction": "inbound", "limit": POLL_PAGE_LIMIT,
                    "since": _iso(since), "fields": "kapso(direction)"}
    items: list = []
    for _page in range(POLL_MAX_PAGES):
        reply = await _request("GET", f"/{cfg.phone_number_id}/messages", params=params)
        data = reply.get("data")
        if isinstance(data, list):
            items.extend(data)
        after = ""
        paging = reply.get("paging")
        if isinstance(paging, dict) and isinstance(paging.get("cursors"), dict):
            after = str(paging["cursors"].get("after") or "")
        if not after or not isinstance(data, list) or len(data) < POLL_PAGE_LIMIT:
            break
        params = {**params, "after": after}
    _last_poll = time.time()
    parsed = [m for m in (parse_inbound(item) for item in items) if m is not None]
    parsed.sort(key=lambda m: (m.at, m.wamid))
    handled = 0
    newest = cursor
    for message in parsed:
        if message.at and message.at < floor:
            continue
        if not _claim(message.wamid, "inbound", message.kind, message.at or now):
            continue
        newest = max(newest, message.at or now)
        if message.sender != cfg.owner_wa_id:
            _ignored_strangers += 1
            log.info("whatsapp: ignored a message from a number ending %s",
                     message.sender[-4:] or "????")
            continue
        _went_hot()
        handled += 1
        try:
            # The owner this sender was matched against — `cfg` was read
            # before the batch, and earlier messages' awaits may have seen
            # the number change since; owner_key() now could be the NEW one.
            await handle(message, owner=cfg.owner_wa_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("whatsapp: handling a message failed", exc_info=True)
    if newest > cursor:
        _state_set("inbound_since", repr(newest))
    return handled


async def handle(message: Inbound, owner: Optional[str] = None) -> None:
    """Act on one message from the OWNER (the caller has checked that, and
    passes the `owner_key` it matched): the policy decides, this line only
    reads it back and replies."""
    me = _this()
    owner = owner if owner is not None else owner_key()
    if message.kind == "button":
        await mark_read(message.wamid)
        await messaging.on_button(me, message.button_id, owner=owner)
        return
    if message.kind != "text":
        await mark_read(message.wamid)
        await messaging.on_other(me)
        return
    await mark_read(message.wamid, typing=True)
    await messaging.on_text(me, message.text, message.wamid, forwarded=message.forwarded,
                            owner=owner)


def _interval(cfg: Config) -> float:
    return cfg.hot_sec if time.monotonic() < _hot_until else cfg.idle_sec


async def _poll_forever() -> None:
    global _errors_in_a_row, _last_error
    while True:
        cfg = config()
        if cfg is None or not cfg.inbound:
            await asyncio.sleep(DEFAULT_IDLE_SEC)
            continue
        try:
            await poll_once()
            _errors_in_a_row = 0
            # The line answered: a card it could not take can go now.
            await messaging.retry_unannounced(_this())
        except asyncio.CancelledError:
            raise
        except ChannelError as e:
            _errors_in_a_row += 1
            _last_error = e.report
            log.warning("whatsapp: poll failed (%d in a row): %s", _errors_in_a_row, e.report)
        except Exception:
            _errors_in_a_row += 1
            log.warning("whatsapp: poll failed (%d in a row)", _errors_in_a_row, exc_info=True)
        delay = _interval(cfg)
        if _errors_in_a_row:
            delay = min(300.0, max(delay, cfg.idle_sec) * (2 ** min(_errors_in_a_row, 5)))
        await asyncio.sleep(delay)


def start(*, poll: bool = True) -> Optional[asyncio.Task]:
    """This line's poller. Returns the task, or None when nothing is to be
    polled. Safe to call unconfigured: the poller sleeps until the
    settings arrive."""
    global _poller
    init_db()
    if not poll:
        return None
    if _poller is not None and not _poller.done():
        return _poller
    _poller = asyncio.create_task(_poll_forever(), name="whatsapp-poll")
    return _poller


async def stop() -> None:
    global _poller
    task, _poller = _poller, None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    await close()


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def status() -> dict:
    """What the settings panel, the preflight check and the diagnostics
    page show. No secrets: the owner's number is masked, the key absent."""
    cfg = config()
    last_in = _last("inbound") if _tables_exist() else None
    last_out = _last("outbound") if _tables_exist() else None
    return {
        "configured": cfg is not None,
        "touched": touched(),
        "missing": missing(),
        "issue": issue(),
        "owner": masked(owner_number()),
        "phone_number_id_set": bool(env("WHATSAPP_PHONE_NUMBER_ID")),
        "template": env("WHATSAPP_TEMPLATE"),
        "inbound": cfg.inbound if cfg else _flag("WHATSAPP_INBOUND", True),
        "approvals": approvals(),
        "voice_notes": voice_notes(),
        "polling": _poller is not None and not _poller.done(),
        "last_poll": _last_poll,
        "last_sent": last_out,
        "last_received": last_in,
        "window_open_until": (last_in + 86400) if last_in else None,
        "last_error": _last_error,
        "ignored_strangers": _ignored_strangers,
        "calls": ("Not available: WhatsApp calls need a WebRTC media stack and a non-US "
                  "business number. Urgent lines go as a voice note instead; a ringing "
                  "phone call is the Twilio provider on the Business desk."),
    }


def _tables_exist() -> bool:
    try:
        with closing(_connect()) as conn:
            return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                                "name='whatsapp_messages'").fetchone() is not None
    except sqlite3.Error:
        return False
